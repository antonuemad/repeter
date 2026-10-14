# deploy/

Production packaging for Repeater on a single VPS (Magic Lap's Hostinger KVM 4), served at
`https://rotator.magic-lap.com`. Step-by-step operator guides are in Arabic in
[`docs/ar/`](../docs/ar/00-deploy.md).

| File | Purpose |
|---|---|
| `docker-compose.yml` | Project `repeater`: `db` (Postgres 16, named volume), `migrate` (one-shot), `web` (127.0.0.1:8090), `worker`, `caddy` (profile `caddy`). Limits keep the stack near 2 vCPU / 6 GB. |
| `Caddyfile` | HTTPS for `$REPEATER_DOMAIN`, 600 MB bodies, long upload timeouts, reverse proxy to `web:8000`. |
| `.env.example` | Every setting with comments. `bootstrap.sh` turns it into `deploy/.env` (mode 600, never committed). |
| `bootstrap.sh` | First install, idempotent, as root: OS check, Docker, `/srv/repeater/{app,media,backups}`, secrets, reverse-proxy detection, build and start, admin, backup cron. |
| `deploy.sh` | Every deploy: build, migrate, `up -d --remove-orphans`, wait for health. Never edits `.env`. |
| `backup.sh` | Daily `pg_dump` to `/srv/repeater/backups` (14 days) and optional `rclone` off-site copy; `restore FILE --yes`. |

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
bash backup.sh restore /srv/repeater/backups/repeater-db-<stamp>.sql.gz --yes
docker compose --env-file .env ps         # status
docker compose --env-file .env logs -f --tail 200 web worker
docker compose --env-file .env exec web python -m repeater.cli check
```

## Reverse proxy

`bootstrap.sh` checks ports 80/443 on the first run (`ss -ltnp`, `docker ps`):

- free: sets `COMPOSE_PROFILES=caddy`; Caddy obtains the certificate (DNS A record must be
  "DNS only" in Cloudflare).
- taken by nginx, Caddy, Traefik, Coolify, Nginx Proxy Manager...: leaves it alone and prints a
  ready snippet that forwards the domain to `127.0.0.1:8090`. When that proxy runs in a container
  it binds the web port to the docker0 address instead (`REPEATER_WEB_BIND`).

## CI/CD (`.github/workflows/`)

- `ci.yml`: ruff, pytest against a Postgres 16 service, shellcheck, compose validation, image build.
- `deploy.yml`: on push to `main` or manually. Runs CI, rsyncs the code over SSH, then
  `bootstrap.sh --stdin-env` on the first deploy or `deploy.sh` afterwards. Secrets travel on SSH
  stdin, never on a command line. Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `REPEATER_DOMAIN`;
  optional `VPS_PORT`, `VPS_KNOWN_HOSTS`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` and platform credentials.
- `server-inspect.yml`: manual, read-only diagnostics (never prints `.env`).

## Building elsewhere

```bash
docker build -t repeater:test .
# behind a TLS-intercepting proxy (the CA is a build secret, not baked in):
docker build --secret id=build_ca,src=/path/to/ca.crt -t repeater:test .
REPEATER_BUILD_CA_FILE=/path/to/ca.crt bash deploy/deploy.sh
```
