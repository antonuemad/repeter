# deploy/

Production packaging for Repeater on a single VPS (Magic Lap's Hostinger KVM 4), served at
`https://rotator.magic-lap.com`. Step-by-step operator guides are in Arabic in
[`docs/ar/`](../docs/ar/00-deploy.md).

| File | Purpose |
|---|---|
| `docker-compose.yml` | Project `repeater`: `db` (Postgres 16, named volume), `migrate` (one-shot), `web` (127.0.0.1:8090), `worker`, `caddy` (profile `caddy`). Limits keep the stack near 2 vCPU / 6 GB. |
| `Caddyfile` | HTTPS for `$REPEATER_DOMAIN`, 600 MB bodies, long upload timeouts, reverse proxy to `web:8000`. |
| `.env.example` | Every setting with comments. `bootstrap.sh` turns it into `deploy/.env` (mode 600, never committed). |
| `bootstrap.sh` | First install, idempotent, as root: OS check, Docker, `/srv/repeater/{app,media,backups}`, secrets, reverse-proxy detection (re-checked on every run; `--redetect-proxy`), build and start, admin, backup cron. |
| `deploy.sh` | Every deploy: (`--pull`: refresh postgres/caddy too), build, pre-deploy dump (kept 30 days), migrate, `up -d --remove-orphans`, wait for health. Never edits `.env`. `:previous` moves only when a build differs. |
| `backup.sh` | Daily `pg_dump` to `/srv/repeater/backups` (14 days), Telegram alert on failure, optional `rclone` off-site copy (dumps copied and expired by age, media mirrored with a trash and a delete cap); `pre-deploy`, `status`, and `restore FILE --yes [--resume]` (one transaction, workspaces paused, web only). |
| `selftest.sh` | Real-container check in a throwaway compose project: atomic restore (also over a newer schema), pause-on-restore, trusted-proxy handling of `X-Forwarded-For`. CI runs it. |

## Layout on the server

```
/srv/repeater/app            code (synced by CI; deploy/.env is excluded from --delete)
/srv/repeater/app/deploy/.env  secrets and settings (600)
/srv/repeater/media          video library, owned by uid 10001 (the image's app user)
/srv/repeater/backups        database dumps (700)
```

## Common commands (as root)

```bash
cd /srv/repeater/app/deploy
bash bootstrap.sh                         # first install (REPEATER_DOMAIN, ADMIN_EMAIL from env)
bash deploy.sh                            # redeploy; --pull, --refresh-ytdlp, --no-build
REPEATER_IMAGE_TAG=previous bash deploy.sh --no-build   # roll back to the previous image
ADMIN_EMAIL=you@magic-lap.com bash bootstrap.sh --create-admin
bash backup.sh                            # backup now
bash backup.sh status                     # age of the last successful backup
bash backup.sh restore /srv/repeater/backups/repeater-db-<stamp>.sql.gz --yes   # pauses every workspace, web only
REPEATER_IMAGE_TAG=previous bash backup.sh restore /srv/repeater/backups/repeater-predeploy-<stamp>.sql.gz --yes   # roll back a schema change
bash selftest.sh repeater-app:latest      # restore/forwarded-header self-test on throwaway containers
docker compose --env-file .env ps         # status
docker compose --env-file .env logs -f --tail 200 web worker
docker compose --env-file .env exec web python -m repeater.cli check
```

After a restore, use the worker start command printed by `backup.sh`: it carries the same
`REPEATER_IMAGE_TAG` used for the restore. Keep that tag on subsequent compose commands too.
Pre-deploy dumps expire after 30 days; retries never prune them by count or overwrite a dump
taken in the same second.

## Reverse proxy

`bootstrap.sh` checks ports 80/443 on the first run and again on every later run (`ss -ltnp`,
`docker ps -a` + `docker inspect` for stopped or restarting containers, `systemctl is-enabled`
for nginx/apache/caddy/traefik/lsws/openresty/haproxy, `/data/coolify`):

- free and nothing else runs on the host: sets `COMPOSE_PROFILES=caddy`; Caddy obtains the
  certificate (DNS A record must be "DNS only" in Cloudflare).
- free but the host runs other containers: does **not** start Caddy by itself (a proxy that is
  down right now would lose its ports); set `REPEATER_PROXY=caddy` to opt in.
- taken by nginx, Caddy, Traefik, Coolify, Nginx Proxy Manager... (running or not): leaves it
  alone and prints a ready snippet that forwards the domain to `127.0.0.1:8090`; the proxy must
  *set* `X-Forwarded-For` to the client address (`$remote_addr`). When that proxy runs in a
  container it binds the web port to the docker0 address instead (`REPEATER_WEB_BIND`).
- an existing `deploy/.env` that does not fit the host (moved from another server) stops the run
  before anything starts; `bootstrap.sh --redetect-proxy` decides again.

The app believes `X-Forwarded-For`/`-Proto` only from loopback and Docker's private ranges
(`REPEATER_FORWARDED_ALLOW_IPS`, passed to uvicorn as `FORWARDED_ALLOW_IPS`), never from `*`.

## CI/CD (`.github/workflows/`)

- `ci.yml`: ruff, pytest against a Postgres 16 service, shellcheck, compose validation, image build
  (`--target app`), the media/import tests inside the built image (`--target test`: the ffmpeg that
  ships), and `selftest.sh` against real containers.
- `deploy.yml`: on push to `main` or manually. Runs CI, rsyncs the code over SSH, then
  `bootstrap.sh --stdin-env` on the first deploy or `deploy.sh` afterwards. Secrets travel on SSH
  stdin, never on a command line. Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `REPEATER_DOMAIN`,
  `VPS_KNOWN_HOSTS` (required: the pinned SSH host key, no trust on first use); optional `VPS_PORT`,
  `REPEATER_PROXY` (first deploy), `ADMIN_EMAIL`, `ADMIN_PASSWORD` and platform credentials.
- `server-inspect.yml`: manual, read-only diagnostics (never prints `.env`).

## Building elsewhere

```bash
docker build -t repeater:test .
# behind a TLS-intercepting proxy (the CA is a build secret, not baked in):
docker build --secret id=build_ca,src=/path/to/ca.crt -t repeater:test .
REPEATER_BUILD_CA_FILE=/path/to/ca.crt bash deploy/deploy.sh
```
