#!/usr/bin/env bash
# Self-test of the deployment files against REAL containers in a throwaway compose project (its own
# network, volume, ports and directories; nothing of a running Repeater is touched). CI runs it
# after building the image; run it by hand after changing backup.sh, docker-compose.yml or the
# Dockerfile:
#
#   bash deploy/selftest.sh [IMAGE]       IMAGE is repeater-app:<tag>, the name compose runs
#                                         (default repeater-app:latest)
#
# What it proves:
#   1. a restore is all-or-nothing: a dump that fails halfway leaves the live database exactly as it
#      was, and a live schema NEWER than the dump (the rollback case) is replaced, not merged
#      (loading the same dump the old way fails and damages the database);
#   2. a restore pauses every workspace and does not start the worker unless --resume is given;
#   3. uvicorn in the image believes X-Forwarded-For only from the trusted proxy ranges, and the
#      leftmost, client-controlled entry is never used.
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${1:-repeater-app:latest}"
PROJECT="repeater-selftest-$$"
WORK="$(mktemp -d)"
FAILED=0

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m  ok\033[0m  %s\n' "$*"; }
fail() {
    printf '\033[1;31mFAIL\033[0m  %s\n' "$*" >&2
    FAILED=1
}
check() {
    local what="$1"
    shift
    if "$@"; then ok "$what"; else fail "$what"; fi
}

command -v docker >/dev/null || {
    echo "docker is required" >&2
    exit 1
}
[[ "$IMAGE" == repeater-app:* ]] || {
    echo "the image must be named repeater-app:<tag>, the name docker-compose.yml runs (docker tag ... first)" >&2
    exit 1
}
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "image $IMAGE not found (build it: docker build -t repeater-app:latest .)" >&2
    exit 1
}
export REPEATER_IMAGE_TAG="${IMAGE#repeater-app:}"

DEPLOY="$WORK/deploy"
mkdir -p "$DEPLOY" "$WORK/media" "$WORK/backups"
cp "$SRC_DIR"/{backup.sh,docker-compose.yml,Caddyfile,.env.example} "$DEPLOY/"
chmod 777 "$WORK/media"
WEB_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
token_key="k1:$(openssl rand -base64 32 | tr -d '\n=' | tr '+/' '-_')"
{
    cat "$DEPLOY/.env.example"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)"
    echo "REPEATER_SECRET_KEY=$(openssl rand -hex 32)"
    echo "REPEATER_TOKEN_ENCRYPTION_KEYS=$token_key"
    echo "REPEATER_MEDIA_DIR=$WORK/media"
    echo "REPEATER_BACKUP_DIR=$WORK/backups"
    echo "REPEATER_WEB_PORT=$WEB_PORT"
    echo "COMPOSE_PROFILES="
} >"$DEPLOY/.env"
chmod 600 "$DEPLOY/.env"

export COMPOSE_PROJECT_NAME="$PROJECT"
export REPEATER_BACKUP_DIR="$WORK/backups"
compose() { docker compose --project-directory "$DEPLOY" -f "$DEPLOY/docker-compose.yml" --env-file "$DEPLOY/.env" "$@"; }
backup() { bash "$DEPLOY/backup.sh" "$@"; }
psql_q() { compose exec -T db psql -U repeater -d repeater -At -v ON_ERROR_STOP=1 -c "$1"; }
running() { [[ -n "$(compose ps --status running -q "$1" 2>/dev/null)" ]]; }

cleanup() {
    log "cleaning up the throwaway project $PROJECT"
    docker rm -f "${PROJECT}-echo" >/dev/null 2>&1 || true
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# --------------------------------------------------------------------------- database scenarios

log "starting the database and applying the migrations ($IMAGE)"
compose up -d --wait db >/dev/null
compose run --rm --no-TTY migrate >/dev/null
compose run --rm --no-TTY migrate python -c '
from repeater.db import session_scope
from repeater.models import Job, Workspace
with session_scope() as s:
    s.add(Workspace(name="Selftest", paused=False))
    s.add(Job(kind="publish_post", payload={"post_id": 1}, dedupe_key="selftest"))
' >/dev/null
check "a workspace is live and a publish job is queued before the dump" \
    test "$(psql_q "select count(*) from workspaces where not paused")|$(psql_q "select count(*) from jobs where status = 'queued'")" = "1|1"
head_revision="$(psql_q "select version_num from alembic_version")"

log "backup.sh writes a dump and the last-success marker"
backup
dump="$(find "$WORK/backups" -maxdepth 1 -name 'repeater-db-*.sql.gz' | head -n 1)"
check "the dump exists" test -s "$dump"
check "the last-success marker exists" test -s "$WORK/backups/status/last_success"
check "backup.sh status is fresh" backup status

log "the live schema becomes NEWER than the dump (a release added a table with a foreign key)"
psql_q "create table video_stats (id serial primary key, video_id int references videos(id))" >/dev/null
psql_q "insert into workspaces (name, timezone, locale, paused, cross_platform_gap_minutes, horizon_hours) values ('after-the-dump', 'UTC', 'en', false, 60, 48)" >/dev/null
fk_before="$(psql_q "select count(*) from pg_constraint where contype = 'f'")"
index_before="$(psql_q "select count(*) from pg_indexes where schemaname = 'public'")"

log "the old way (psql -v ON_ERROR_STOP=1 on the live database) fails and damages a copy of it"
compose stop web worker >/dev/null 2>&1 || true
psql_q "create database selftest_old template repeater" >/dev/null
old_status=0
gzip -dc "$dump" | compose exec -T db psql -U repeater -d selftest_old -v ON_ERROR_STOP=1 -q >/dev/null 2>&1 || old_status=$?
check "loading the dump onto a newer schema fails the old way (exit $old_status)" test "$old_status" -ne 0
check "... and leaves that copy with fewer foreign keys than before" \
    test "$(compose exec -T db psql -U repeater -d selftest_old -At -c "select count(*) from pg_constraint where contype = 'f'")" -lt "$fk_before"
psql_q "drop database selftest_old" >/dev/null

log "a dump that fails halfway is rolled back completely"
{
    gzip -dc "$dump"
    echo 'SELECT 1/0;'
} | gzip >"$WORK/broken.sql.gz"
broken_status=0
backup restore "$WORK/broken.sql.gz" --yes >/dev/null 2>&1 || broken_status=$?
check "restoring the broken dump fails (exit $broken_status)" test "$broken_status" -ne 0
check "the live database is untouched: same foreign keys" \
    test "$(psql_q "select count(*) from pg_constraint where contype = 'f'")" = "$fk_before"
check "... same indexes" test "$(psql_q "select count(*) from pg_indexes where schemaname = 'public'")" = "$index_before"
check "... the newer table and the later row are still there" \
    test "$(psql_q "select count(*) from video_stats")|$(psql_q "select count(*) from workspaces")" = "0|2"
if running worker || running web; then fail "web or worker was started after a failed restore"; else ok "web and worker stay stopped after a failed restore"; fi

log "restoring the good dump over the newer schema"
backup restore "$dump" --yes >"$WORK/restore.log" 2>&1 || {
    cat "$WORK/restore.log" >&2
    fail "the restore of the good dump failed"
}
check "the newer table is gone: the schema is exactly the dump's" \
    test "$(psql_q "select count(*) from information_schema.tables where table_schema = 'public' and table_name = 'video_stats'")" = "0"
check "the row added after the dump is gone" test "$(psql_q "select count(*) from workspaces")" = "1"
check "the migration level is the dump's (head)" test "$(psql_q "select version_num from alembic_version")" = "$head_revision"
check "every workspace is paused" test "$(psql_q "select bool_and(paused) from workspaces")" = "t"
check "the queued publish job is still in the queue (nothing ran)" test "$(psql_q "select count(*) from jobs where status = 'queued'")" = "1"
if running worker; then fail "the worker was started without --resume"; else ok "the worker is not running after a restore without --resume"; fi
if running web; then ok "web is running"; else fail "web did not start"; fi
check "a safety dump of the replaced database was kept" \
    test -n "$(find "$WORK/backups" -maxdepth 1 -name 'repeater-prerestore-*.sql.gz' | head -n 1)"
check "the output says all workspaces are paused" grep -q 'ALL WORKSPACES ARE PAUSED' "$WORK/restore.log"

log "restore --resume starts the worker, and the workspaces stay paused"
psql_q "update workspaces set paused = false" >/dev/null
backup restore "$dump" --yes --resume >"$WORK/resume.log" 2>&1 || {
    cat "$WORK/resume.log" >&2
    fail "restore --resume failed"
}
if running worker; then ok "the worker runs with --resume"; else fail "the worker is not running after --resume"; fi
check "... and every workspace is still paused" test "$(psql_q "select bool_and(paused) from workspaces")" = "t"
check "the web container carries the trusted-proxy list, not '*'" \
    bash -c "[[ \"\$(docker exec \"\$(docker compose --project-directory '$DEPLOY' -f '$DEPLOY/docker-compose.yml' --env-file '$DEPLOY/.env' ps -q web)\" printenv FORWARDED_ALLOW_IPS)\" == *172.16.0.0/12* ]]"
check "the image's command line has no --forwarded-allow-ips" \
    bash -c "! docker image inspect --format '{{json .Config.Cmd}}' '$IMAGE' | grep -q forwarded-allow-ips"

# --------------------------------------------------------------------------- forwarded headers

log "X-Forwarded-For from a trusted proxy (two containers on the compose network)"
mkdir -p "$WORK/echo"
cat >"$WORK/echo/echo_app.py" <<'PY'
async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = f"{scope['client'][0]} {scope['scheme']}".encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})
PY
network="${PROJECT}_backend"
compose_allow="$(compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["web"]["environment"]["FORWARDED_ALLOW_IPS"])')"

# Starts the echo server with the given trusted list and asks it, from a second container, who the
# client is when the request claims "X-Forwarded-For: 6.6.6.6, 203.0.113.9" (6.6.6.6 is what a
# client typed; 203.0.113.9 is what the proxy appended).
client_seen_by_app() {
    docker rm -f "${PROJECT}-echo" >/dev/null 2>&1 || true
    docker run -d --name "${PROJECT}-echo" --network "$network" -e "FORWARDED_ALLOW_IPS=$1" \
        -v "$WORK/echo:/echo:ro" --entrypoint uvicorn "$IMAGE" \
        --app-dir /echo echo_app:app --host 0.0.0.0 --port 8000 --proxy-headers >/dev/null
    docker run --rm --network "$network" --entrypoint python "$IMAGE" -c '
import sys, time, urllib.request
req = urllib.request.Request(
    "http://'"${PROJECT}"'-echo:8000/",
    headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9", "X-Forwarded-Proto": "https"},
)
for _ in range(40):
    try:
        print(urllib.request.urlopen(req, timeout=3).read().decode())
        break
    except OSError:
        time.sleep(0.5)
else:
    sys.exit("no answer from the echo server")
'
}
default_seen="$(client_seen_by_app "$compose_allow")"
check "default trusted ranges: the proxy-appended address wins, the client-typed one is ignored ($default_seen)" \
    test "$default_seen" = "203.0.113.9 https"
loopback_seen="$(client_seen_by_app "127.0.0.1")"
check "loopback only: a peer on the compose network is not trusted, the header is ignored ($loopback_seen)" \
    bash -c "[[ '$loopback_seen' != 203.0.113.9* && '$loopback_seen' != 6.6.6.6* && '$loopback_seen' == *' http' ]]"
star_seen="$(client_seen_by_app '*')"
check "for contrast, '*' (the old setting) believes the client-typed address ($star_seen)" \
    test "$star_seen" = "6.6.6.6 https"

echo
if ((FAILED)); then
    echo "SELF-TEST FAILED" >&2
    exit 1
fi
log "self-test passed"
