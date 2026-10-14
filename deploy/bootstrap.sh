#!/usr/bin/env bash
# Repeater first-time setup on the VPS. Idempotent: safe to run again at any time.
#
# Run as root from the app directory (copy or clone the repository there first):
#   sudo REPEATER_DOMAIN=rotator.magic-lap.com ADMIN_EMAIL=you@magic-lap.com \
#        bash /srv/repeater/app/deploy/bootstrap.sh
# The admin password is asked on the terminal (or read from stdin with --stdin-env).
#
# Modes:
#   (default)       verify the OS, install Docker if missing, create /srv/repeater/{app,media,backups},
#                   create deploy/.env with generated secrets, pick the reverse proxy mode, build and
#                   start, create the admin, install the daily backup cron, print a checklist.
#   --update-env    only merge platform credentials into deploy/.env (needs --stdin-env).
#   --create-admin  only create/update the dashboard admin (the stack must be running).
# Options:
#   --stdin-env     read KEY=VALUE lines from stdin: ADMIN_EMAIL, ADMIN_PASSWORD, REPEATER_DOMAIN
#                   and the platform credentials in UPDATABLE_KEYS. Empty values are ignored.
#
# Never stops or reconfigures other services on the host: when ports 80/443 are taken, it
# prints the snippet to add to the existing reverse proxy instead.
set -Eeuo pipefail

ROOT_DIR="${REPEATER_ROOT:-/srv/repeater}"
TARGET_APP_DIR="$ROOT_DIR/app"
MEDIA_DIR_DEFAULT="$ROOT_DIR/media"
BACKUP_DIR="$ROOT_DIR/backups"
APP_UID=10001
DEFAULT_DOMAIN="rotator.magic-lap.com"
CRON_FILE="${REPEATER_CRON_FILE:-/etc/cron.d/repeater-backup}"
# Credentials that --update-env (and CI) may write into deploy/.env. Generated secrets are not here
# on purpose: replacing them would lock the database or make stored OAuth tokens unreadable.
UPDATABLE_KEYS=(
    REPEATER_TIKTOK_CLIENT_KEY REPEATER_TIKTOK_CLIENT_SECRET
    REPEATER_GOOGLE_CLIENT_ID REPEATER_GOOGLE_CLIENT_SECRET
    REPEATER_META_APP_ID REPEATER_META_APP_SECRET
    REPEATER_TELEGRAM_BOT_TOKEN REPEATER_TELEGRAM_CHAT_ID
    REPEATER_YTDLP_PROXY
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

declare -A UPDATES=()
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
REPEATER_DOMAIN="${REPEATER_DOMAIN:-}"
ENV_CREATED=0

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
    exit 1
}

# --------------------------------------------------------------------------- .env helpers

# Print the value of KEY from an env file (last uncommented assignment, quotes removed).
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

# Quote a value for a compose env file: plain when safe, single quotes otherwise.
env_quote() {
    local value="$1"
    if [[ "$value" =~ ^[A-Za-z0-9_@%+=:,./-]*$ ]]; then
        printf '%s' "$value"
    elif [[ "$value" != *"'"* && "$value" != *$'\n'* && "$value" != *$'\r'* ]]; then
        printf "'%s'" "$value"
    else
        return 1
    fi
}

# Set KEY=VALUE in an env file (replacing every uncommented assignment), atomically, mode 600.
env_set() {
    local file="$1" key="$2" value="$3" quoted tmp
    quoted="$(env_quote "$value")" || die "the value for $key contains a single quote or a line break; set it by hand in $file"
    tmp="$(mktemp "$file.XXXXXX")"
    KEY="$key" LINE="$key=$quoted" awk '
        index($0, ENVIRON["KEY"] "=") == 1 { if (!done) print ENVIRON["LINE"]; done = 1; next }
        { print }
        END { if (!done) print ENVIRON["LINE"] }' "$file" >"$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$file"
}

gen_urlsafe() { openssl rand -base64 "$1" | tr -d '\n=' | tr '+/' '-_'; }
gen_secret_key() { gen_urlsafe 48; }
gen_token_key_spec() { printf 'k1:%s' "$(gen_urlsafe 32)"; }
gen_db_password() { openssl rand -hex 32; }

valid_domain() { [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; }

is_updatable_key() {
    local k
    for k in "${UPDATABLE_KEYS[@]}"; do [[ "$k" == "$1" ]] && return 0; done
    return 1
}

# Read KEY=VALUE lines from stdin into ADMIN_*, REPEATER_DOMAIN and UPDATES.
read_stdin_env() {
    local line key value
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] || die "stdin line without '=' (expected KEY=VALUE)"
        key="${line%%=*}"
        value="${line#*=}"
        [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "invalid key name on stdin"
        [[ -z "$value" ]] && continue
        case "$key" in
            ADMIN_EMAIL) ADMIN_EMAIL="$value" ;;
            ADMIN_PASSWORD) ADMIN_PASSWORD="$value" ;;
            REPEATER_DOMAIN) REPEATER_DOMAIN="$value" ;;
            *)
                is_updatable_key "$key" || die "$key cannot be set through stdin (allowed: ${UPDATABLE_KEYS[*]})"
                UPDATES["$key"]="$value"
                ;;
        esac
    done
}

# Write UPDATES into the env file; prints the names (never the values) of changed keys.
apply_updates() {
    local file="$1" key changed=0
    for key in "${!UPDATES[@]}"; do
        if [[ "$(env_get "$key" "$file")" != "${UPDATES[$key]}" ]]; then
            env_set "$file" "$key" "${UPDATES[$key]}"
            log "updated $key in deploy/.env"
            changed=$((changed + 1))
        fi
    done
    ((changed > 0)) || log "deploy/.env already up to date"
}

# --------------------------------------------------------------------------- host checks

require_root() { [[ "$(id -u)" -eq 0 ]] || die "run as root: sudo bash $0"; }

check_os() {
    [[ -r /etc/os-release ]] || die "cannot read /etc/os-release; Ubuntu 22.04+ or Debian 12+ is required"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}:${ID_LIKE:-}" in
        ubuntu:* | debian:* | *:*debian*) ;;
        *) die "unsupported OS '${PRETTY_NAME:-unknown}'; Ubuntu 22.04+ or Debian 12+ is required" ;;
    esac
    case "$(uname -m)" in
        x86_64 | aarch64) ;;
        *) die "unsupported CPU architecture $(uname -m)" ;;
    esac
    log "OS: ${PRETTY_NAME:-$ID} ($(uname -m)), $(nproc) vCPU, $(free -g | awk '/^Mem:/ {print $2}') GB RAM"
    local free_gb
    free_gb="$(df -BG --output=avail "$(dirname "$ROOT_DIR")" 2>/dev/null | tail -1 | tr -dc '0-9')"
    if [[ -n "$free_gb" ]] && ((free_gb < 20)); then
        warn "only ${free_gb} GB free under $(dirname "$ROOT_DIR"); the video library needs room to grow"
    fi
}

apt_install() {
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null
}

install_host_tools() {
    local missing=()
    command -v curl >/dev/null || missing+=(curl)
    command -v openssl >/dev/null || missing+=(openssl)
    command -v ss >/dev/null || missing+=(iproute2)
    command -v flock >/dev/null || missing+=(util-linux)
    [[ -d /etc/cron.d ]] || missing+=(cron)
    dpkg -s ca-certificates >/dev/null 2>&1 || missing+=(ca-certificates)
    if ((${#missing[@]} > 0)); then
        log "installing host tools: ${missing[*]}"
        apt_install "${missing[@]}"
    fi
}

version_major() { printf '%s' "${1#v}" | cut -d. -f1 | tr -dc '0-9'; }

install_docker() {
    if ! command -v docker >/dev/null; then
        log "installing Docker Engine with the official convenience script (get.docker.com)"
        local script
        script="$(mktemp)"
        curl -fsSL --retry 3 --max-time 120 https://get.docker.com -o "$script"
        sh "$script"
        rm -f "$script"
    fi
    systemctl enable --now docker >/dev/null 2>&1 || true
    if ! docker compose version >/dev/null 2>&1; then
        log "installing the Docker Compose plugin"
        apt_install docker-compose-plugin ||
            die "Docker is installed but the compose plugin is missing; install 'docker-compose-plugin' (https://docs.docker.com/compose/install/linux/)"
    fi
    local engine compose_version
    engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || die "the Docker daemon is not running (systemctl status docker)"
    compose_version="$(docker compose version --short 2>/dev/null)"
    local major minor
    major="$(version_major "$engine")"
    ((${major:-0} >= 25)) || die "Docker Engine $engine is too old; 25 or newer is required (BuildKit secrets, health check start_interval)"
    major="$(version_major "$compose_version")"
    minor="$(printf '%s' "$compose_version" | cut -d. -f2 | tr -dc '0-9')"
    if ((${major:-0} < 2 || (${major:-0} == 2 && ${minor:-0} < 24))); then
        die "Docker Compose ${compose_version:-unknown} is too old; 2.24 or newer is required (env_file 'required: false')"
    fi
    log "Docker Engine $engine, Compose $compose_version"
}

prepare_dirs() {
    install -d -m 0755 "$ROOT_DIR" "$TARGET_APP_DIR"
    install -d -m 0700 "$BACKUP_DIR"
    local media
    media="$(env_get REPEATER_MEDIA_DIR "$ENV_FILE")"
    media="${media:-$MEDIA_DIR_DEFAULT}"
    install -d -m 0750 "$media"
    chown "$APP_UID:$APP_UID" "$media"
}

# The app is expected in $TARGET_APP_DIR; when run from another checkout, copy it there and re-run.
place_code() {
    install -d -m 0755 "$ROOT_DIR" "$TARGET_APP_DIR"
    [[ "$APP_DIR" == "$(cd "$TARGET_APP_DIR" && pwd)" ]] && return 0
    log "copying the app from $APP_DIR to $TARGET_APP_DIR"
    tar -C "$APP_DIR" \
        --exclude=./.git --exclude=./.venv --exclude=./deploy/.env --exclude='*.egg-info' \
        --exclude='__pycache__' --exclude=./.pytest_cache --exclude=./.ruff_cache --exclude=./media \
        -cf - . | tar -C "$TARGET_APP_DIR" --no-same-owner -xf -
    if [[ -f "$APP_DIR/deploy/.env" && ! -f "$TARGET_APP_DIR/deploy/.env" ]]; then
        install -m 0600 "$APP_DIR/deploy/.env" "$TARGET_APP_DIR/deploy/.env"
    fi
    exec bash "$TARGET_APP_DIR/deploy/bootstrap.sh" "$@"
}

# --------------------------------------------------------------------------- env file

pg_volume_exists() { docker volume inspect repeater_pg_data >/dev/null 2>&1; }

create_env_file() {
    [[ -f "$ENV_FILE" ]] && return 0
    [[ -f "$SCRIPT_DIR/.env.example" ]] || die "missing $SCRIPT_DIR/.env.example"
    install -m 0600 "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    env_set "$ENV_FILE" REPEATER_MEDIA_DIR "$MEDIA_DIR_DEFAULT"
    ENV_CREATED=1
    log "created deploy/.env from .env.example"
}

fill_secrets() {
    if [[ -z "$(env_get POSTGRES_PASSWORD "$ENV_FILE")" ]]; then
        if pg_volume_exists; then
            die "POSTGRES_PASSWORD is empty but the database volume repeater_pg_data already exists; restore the previous deploy/.env (a new password would lock the database out)"
        fi
        env_set "$ENV_FILE" POSTGRES_PASSWORD "$(gen_db_password)"
        log "generated POSTGRES_PASSWORD"
    fi
    if [[ -z "$(env_get REPEATER_SECRET_KEY "$ENV_FILE")" ]]; then
        env_set "$ENV_FILE" REPEATER_SECRET_KEY "$(gen_secret_key)"
        log "generated REPEATER_SECRET_KEY"
    fi
    if [[ -z "$(env_get REPEATER_TOKEN_ENCRYPTION_KEYS "$ENV_FILE")" ]]; then
        if pg_volume_exists; then
            warn "generating a new token encryption key for an existing database: previously connected accounts must be reconnected"
        fi
        env_set "$ENV_FILE" REPEATER_TOKEN_ENCRYPTION_KEYS "$(gen_token_key_spec)"
        log "generated REPEATER_TOKEN_ENCRYPTION_KEYS (keep a copy of deploy/.env somewhere safe)"
    fi
}

set_domain() {
    local current domain
    current="$(env_get REPEATER_DOMAIN "$ENV_FILE")"
    if ((ENV_CREATED)) || [[ -z "$current" ]]; then
        domain="${REPEATER_DOMAIN:-${current:-$DEFAULT_DOMAIN}}"
    else
        domain="$current"
        if [[ -n "$REPEATER_DOMAIN" && "$REPEATER_DOMAIN" != "$current" ]]; then
            warn "keeping REPEATER_DOMAIN=$current from deploy/.env (edit it there to change the domain, together with REPEATER_BASE_URL and every OAuth redirect URI)"
        fi
    fi
    valid_domain "$domain" || die "invalid domain '$domain'"
    env_set "$ENV_FILE" REPEATER_DOMAIN "$domain"
    if ((ENV_CREATED)) || [[ -z "$(env_get REPEATER_BASE_URL "$ENV_FILE")" ]]; then
        env_set "$ENV_FILE" REPEATER_BASE_URL "https://$domain"
    fi
    DOMAIN="$domain"
}

# --------------------------------------------------------------------------- reverse proxy detection

# Listeners on 80/443 as "process" names (docker-proxy for published container ports).
port_80_443_processes() {
    ss -H -ltnp '( sport = :80 or sport = :443 )' 2>/dev/null |
        grep -oE '"[^"]+",pid=' | cut -d'"' -f2 | sort -u || true
}

# Containers publishing 80/443 as "name|image|compose project".
port_80_443_containers() {
    command -v docker >/dev/null || return 0
    docker ps --format '{{.Names}}|{{.Image}}|{{.Label "com.docker.compose.project"}}|{{.Ports}}' 2>/dev/null |
        awk -F'|' '$4 ~ /:(80|443)->/ {print $1 "|" $2 "|" $3}' || true
}

# Prints "<kind> <description>": none, repeater-caddy, docker-traefik, docker-npm, docker-caddy,
# docker-nginx, docker-other, host-nginx, host-caddy, host-traefik, host-apache, host-other.
detect_proxy() {
    local containers processes name image project
    containers="$(port_80_443_containers)"
    processes="$(port_80_443_processes)"
    if [[ -n "$containers" ]]; then
        IFS='|' read -r name image project <<<"$(head -1 <<<"$containers")"
        local id="${name} ${image}"
        id="${id,,}"
        if [[ "$project" == repeater ]]; then
            echo "repeater-caddy container $name"
        elif [[ "$id" == *traefik* || "$id" == *coolify-proxy* ]]; then
            echo "docker-traefik container $name ($image)"
        elif [[ "$id" == *nginx-proxy-manager* ]]; then
            echo "docker-npm container $name ($image)"
        elif [[ "$id" == *caddy* ]]; then
            echo "docker-caddy container $name ($image)"
        elif [[ "$id" == *nginx* || "$id" == *openresty* ]]; then
            echo "docker-nginx container $name ($image)"
        else
            echo "docker-other container $name ($image)"
        fi
        return 0
    fi
    local procs
    procs="$({ grep -v '^docker-proxy$' <<<"$processes" || true; } | tr '\n' ' ' | sed 's/ *$//')"
    if [[ -z "$processes" ]]; then
        echo "none ports 80/443 are free"
    elif [[ -z "$procs" ]]; then
        echo "docker-other a container (docker-proxy)"
    elif [[ " $procs " == *" nginx "* ]]; then
        echo "host-nginx host process nginx"
    elif [[ " $procs " == *" caddy "* ]]; then
        echo "host-caddy host process caddy"
    elif [[ " $procs " == *" traefik "* ]]; then
        echo "host-traefik host process traefik"
    elif [[ " $procs " == *" apache2 "* || " $procs " == *" httpd "* ]]; then
        echo "host-apache host process $procs"
    else
        echo "host-other host process $procs"
    fi
}

docker_bridge_ip() {
    local ip
    ip="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
    printf '%s' "${ip:-172.17.0.1}"
}

port_in_use() { [[ -n "$(ss -H -ltn "( sport = :$1 )" 2>/dev/null)" ]]; }

first_free_port() {
    local port
    for port in $(seq "$1" "$2"); do
        port_in_use "$port" || {
            echo "$port"
            return 0
        }
    done
    return 1
}

# Decide (first run) or report (later runs) how HTTPS reaches the web container.
configure_proxy() {
    local detected kind desc profiles
    detected="$(detect_proxy)"
    kind="${detected%% *}"
    desc="${detected#* }"
    profiles="$(env_get COMPOSE_PROFILES "$ENV_FILE")"
    if ((ENV_CREATED)); then
        local port
        port="$(env_get REPEATER_WEB_PORT "$ENV_FILE")"
        if port_in_use "${port:-8090}"; then
            port="$(first_free_port 8091 8199)" || die "no free port in 8091-8199 for the web container"
            env_set "$ENV_FILE" REPEATER_WEB_PORT "$port"
            log "port 8090 is taken; the web container uses 127.0.0.1:$port"
        fi
        case "$kind" in
            none | repeater-caddy)
                env_set "$ENV_FILE" COMPOSE_PROFILES caddy
                profiles=caddy
                log "ports 80/443 are free: Repeater runs its own Caddy with automatic HTTPS"
                ;;
            docker-*)
                env_set "$ENV_FILE" COMPOSE_PROFILES ""
                env_set "$ENV_FILE" REPEATER_WEB_BIND "$(docker_bridge_ip)"
                profiles=""
                log "ports 80/443 belong to $desc: the web container listens on the docker0 address for it"
                ;;
            *)
                env_set "$ENV_FILE" COMPOSE_PROFILES ""
                profiles=""
                log "ports 80/443 belong to $desc: Repeater will not touch it"
                ;;
        esac
    fi
    PROXY_KIND="$kind"
    PROXY_DESC="$desc"
    if [[ ",$profiles," == *",caddy,"* ]]; then
        PROXY_MODE=caddy
        if [[ "$kind" != none && "$kind" != repeater-caddy ]]; then
            warn "COMPOSE_PROFILES=caddy but ports 80/443 are used by $desc; Caddy will fail to start. Set COMPOSE_PROFILES= in deploy/.env and use the snippet below instead."
            print_proxy_snippet
        fi
    else
        PROXY_MODE=external
        if [[ "$kind" == none ]]; then
            warn "ports 80/443 are free but COMPOSE_PROFILES does not include caddy: set COMPOSE_PROFILES=caddy in deploy/.env to let Repeater serve HTTPS itself"
        fi
    fi
}

print_proxy_snippet() {
    local host port
    host="$(env_get REPEATER_WEB_BIND "$ENV_FILE")"
    host="${host:-127.0.0.1}"
    port="$(env_get REPEATER_WEB_PORT "$ENV_FILE")"
    port="${port:-8090}"
    local upstream="http://$host:$port"
    cat <<EOF

------------------------------------------------------------------------------------------
Ports 80/443 are served by: $PROXY_DESC
Add $DOMAIN to that proxy, forwarding to $upstream (nothing was changed on it).
Requirements: request bodies up to 600 MB, 1 hour timeouts, X-Forwarded-Proto/For passed.
EOF
    case "$PROXY_KIND" in
        host-nginx | docker-nginx | docker-npm)
            [[ "$PROXY_KIND" == docker-npm ]] &&
                echo "Nginx Proxy Manager: add a Proxy Host $DOMAIN -> $upstream, request a Let's Encrypt certificate, and put the lines below in its Advanced tab."
            cat <<EOF
--- nginx: /etc/nginx/sites-available/repeater.conf (then nginx -t && systemctl reload nginx;
--- HTTPS with: certbot --nginx -d $DOMAIN)
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 600m;
    proxy_request_buffering off;
    location / {
        proxy_pass $upstream;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 10s;
        proxy_send_timeout 3600s;
        proxy_read_timeout 600s;
    }
}
EOF
            ;;
        host-caddy | docker-caddy)
            cat <<EOF
--- Caddyfile (then: caddy reload)
$DOMAIN {
    request_body {
        max_size 600MB
    }
    reverse_proxy $upstream {
        transport http {
            response_header_timeout 5m
        }
    }
}
EOF
            ;;
        host-traefik | docker-traefik)
            cat <<EOF
--- Traefik dynamic configuration (file provider), e.g. repeater.yaml.
--- Coolify: Servers -> Proxy -> Dynamic Configurations. Use your own entry point and
--- certificate resolver names (Coolify's defaults are "https" and "letsencrypt").
http:
  routers:
    repeater:
      rule: "Host(\`$DOMAIN\`)"
      entryPoints: ["https"]
      service: repeater
      tls:
        certResolver: letsencrypt
  services:
    repeater:
      loadBalancer:
        servers:
          - url: "$upstream"
EOF
            ;;
        *)
            echo "--- Configure a reverse proxy for https://$DOMAIN -> $upstream with the requirements above."
            ;;
    esac
    if [[ "$PROXY_KIND" == docker-* ]] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
        echo "ufw is active: if the proxy container cannot reach $upstream, allow it with:"
        echo "  ufw allow in on docker0 to $host port $port proto tcp   (and the proxy's own bridge network)"
    fi
    echo "------------------------------------------------------------------------------------------"
}

# --------------------------------------------------------------------------- start, admin, cron

compose() {
    docker compose --project-directory "$SCRIPT_DIR" -f "$SCRIPT_DIR/docker-compose.yml" --env-file "$ENV_FILE" "$@"
}

prompt_admin_password() {
    [[ -n "$ADMIN_EMAIL" && -z "$ADMIN_PASSWORD" && -t 0 ]] || return 0
    local again
    read -rsp "Password for $ADMIN_EMAIL (10+ characters): " ADMIN_PASSWORD
    echo
    read -rsp "Repeat the password: " again
    echo
    [[ "$ADMIN_PASSWORD" == "$again" ]] || die "the passwords do not match"
}

create_admin() {
    if [[ -z "$ADMIN_EMAIL" || -z "$ADMIN_PASSWORD" ]]; then
        log "no ADMIN_EMAIL/ADMIN_PASSWORD given: create the admin later with"
        echo "    sudo ADMIN_EMAIL=you@magic-lap.com bash $SCRIPT_DIR/bootstrap.sh --create-admin"
        return 0
    fi
    [[ "$ADMIN_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "ADMIN_EMAIL is not an email address"
    ((${#ADMIN_PASSWORD} >= 10)) || die "ADMIN_PASSWORD must be at least 10 characters"
    log "creating/updating the dashboard admin $ADMIN_EMAIL"
    # The password goes through stdin, never through a command line visible in `ps`.
    local output status=0
    output="$(printf '%s\n' "$ADMIN_PASSWORD" |
        compose exec -T web python -m repeater.cli create-admin --email "$ADMIN_EMAIL" --password-stdin 2>&1)" ||
        status=$?
    sed -e '/GetPassWarning/d' -e '/fallback_getpass/d' -e '/Password input may be echoed/d' \
        -e 's/^Password: *//' -e '/^[[:space:]]*$/d' <<<"$output"
    ((status == 0)) || die "creating the admin failed (see the message above)"
}

install_backup_cron() {
    local content
    content="$(
        cat <<EOF
# Repeater: daily database backup at 03:17 (server time). Logs: journalctl -t repeater-backup
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 3 * * * root REPEATER_BACKUP_DIR=$BACKUP_DIR bash $TARGET_APP_DIR/deploy/backup.sh 2>&1 | logger -t repeater-backup
EOF
    )"
    if [[ ! -f "$CRON_FILE" || "$(cat "$CRON_FILE")" != "$content" ]]; then
        printf '%s\n' "$content" >"$CRON_FILE"
        chmod 0644 "$CRON_FILE"
        log "installed the daily backup cron job ($CRON_FILE)"
    fi
    systemctl enable --now cron >/dev/null 2>&1 || true
}

public_ip() {
    ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}'
}

print_checklist() {
    local ip base
    ip="$(public_ip)"
    base="https://$DOMAIN"
    cat <<EOF

==========================================================================================
Repeater is running. Remaining steps (details in docs/ar/00-deploy.md):

1. DNS (Cloudflare -> magic-lap.com -> DNS): A record  name: ${DOMAIN%%.*}  content: ${ip:-<VPS IPv4>}
   Proxy status: DNS only (grey cloud). Then open $base/healthz
2. Platform apps (docs/ar/01-03), then put the keys in $ENV_FILE (or GitHub secrets)
   and run: sudo bash $SCRIPT_DIR/deploy.sh
     TikTok redirect URI:  $base/oauth/tiktok/callback
     Google redirect URI:  $base/oauth/google/callback
     Meta redirect URI:    $base/oauth/meta/callback
     Meta deauthorize:     $base/meta/deauthorize
     Meta data deletion:   $base/meta/data-deletion
     Privacy policy:       $base/privacy
     Terms of service:     $base/terms
3. Keep a copy of $ENV_FILE in a password manager (it holds the token encryption key).
4. Backups: daily at 03:17 into $BACKUP_DIR; set RCLONE_REMOTE in deploy/.env for an off-site copy.
EOF
    if [[ "$PROXY_MODE" == external ]]; then
        echo "5. Reverse proxy: add $DOMAIN to the existing proxy (snippet above)."
    fi
    echo "=========================================================================================="
}

# --------------------------------------------------------------------------- main

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

main() {
    local mode=full stdin_env=0
    local -a args=("$@")
    while (($# > 0)); do
        case "$1" in
            --update-env) mode=update-env ;;
            --create-admin) mode=create-admin ;;
            --stdin-env) stdin_env=1 ;;
            -h | --help)
                usage
                return 0
                ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
        shift
    done
    require_root

    case "$mode" in
        update-env)
            ((stdin_env)) || die "--update-env reads KEY=VALUE lines from stdin; add --stdin-env"
            [[ -f "$ENV_FILE" ]] || die "deploy/.env does not exist yet; run bootstrap.sh without --update-env first"
            read_stdin_env
            apply_updates "$ENV_FILE"
            return 0
            ;;
        create-admin)
            if ((stdin_env)); then read_stdin_env; fi
            [[ -f "$ENV_FILE" ]] || die "deploy/.env does not exist yet; run bootstrap.sh first"
            prompt_admin_password
            [[ -n "$ADMIN_EMAIL" && -n "$ADMIN_PASSWORD" ]] || die "ADMIN_EMAIL and ADMIN_PASSWORD are required"
            create_admin
            return 0
            ;;
    esac

    check_os
    install_host_tools
    place_code "${args[@]}"
    if ((stdin_env)); then read_stdin_env; fi
    prompt_admin_password
    install_docker
    create_env_file
    set_domain
    fill_secrets
    apply_updates "$ENV_FILE"
    prepare_dirs
    configure_proxy
    if [[ "$PROXY_MODE" == external ]]; then print_proxy_snippet; fi
    log "building and starting Repeater (the first build takes several minutes)"
    bash "$SCRIPT_DIR/deploy.sh"
    create_admin
    install_backup_cron
    print_checklist
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
