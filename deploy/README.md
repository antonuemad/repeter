# deploy/

Production packaging for Repeater on a single VPS (Magic Lap's Hostinger KVM 4), served at
`https://rotator.magic-lap.com`. Step-by-step operator guides are in Arabic in
[`docs/ar/`](../docs/ar/00-deploy.md).

During the customer testing period, keep `REPEATER_PUBLISHING_ENABLED=false` (the default).
It blocks every workspace's automatic/manual publication and publisher polling, including private
test posts. Account linking and content preparation remain available. Only the user's explicit
permission ending this restriction can authorize enabling publishing and recreating web/worker.

| File | Purpose |
|---|---|
| `docker-compose.yml` | Project `repeater`: `db` (Postgres 16, named volume), `migrate` (one-shot), `web` (127.0.0.1:8090), `worker`, `caddy` (profile `caddy`). Limits keep the stack near 2 vCPU / 6 GB. |
| `Caddyfile` | HTTPS for `$REPEATER_DOMAIN`, 600 MB bodies, long upload timeouts, reverse proxy to `web:8000`. |
| `.env.example` | Every setting with comments. `bootstrap.sh` turns it into `deploy/.env` (mode 600, never committed). |
| `bootstrap.sh` | First install, idempotent, as root: OS check, Docker, `/srv/repeater/{app,media,backups}`, secrets, reverse-proxy detection (re-checked on every run; `--redetect-proxy`), build and start, admin, backup cron. |
| `deploy.sh` | Every deploy: (`--pull`: refresh postgres/caddy too), build, pre-deploy dump (kept 30 days), migrate, `up -d --remove-orphans`, wait for health. Never edits `.env`. `:previous` moves only when a build differs. |
| `backup.sh` | Daily `pg_dump` to `/srv/repeater/backups` (14 days), Telegram alert on failure, optional `rclone` off-site copy (dumps copied and expired by age, media mirrored with a trash and a delete cap); `pre-deploy`, `status`, and `restore FILE --yes [--resume]` (one transaction, workspaces paused, web only). |
| `monitor.py` | Host Python 3 monitor every five minutes: HTTPS, container health, queue delay, disk and separate backup receipts. Telegram failures/recoveries are deduplicated; failed deliveries retry. |
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
docker compose --env-file .env exec web python -m repeater.cli integration-status
docker compose --env-file .env exec web python -m repeater.cli test-alert
python3 monitor.py --no-notify             # inspect without a Telegram message
```

After a restore, use the worker start command printed by `backup.sh`: it carries the same
`REPEATER_IMAGE_TAG` used for the restore. Keep that tag on subsequent compose commands too.
Pre-deploy dumps expire after 30 days; retries never prune them by count or overwrite a dump
taken in the same second.

The operator preflight separates credentials, connected accounts and confirmed publications;
it does not certify live publishing. See [the invited-pilot runbook](../docs/ar/07-pilot-operations.md).
The host monitor cannot detect loss of the entire VPS; an independent uptime service is still
required for that failure. Existing installations must refresh `/etc/cron.d/repeater-backup`
using `install_backup_cron` from `bootstrap.sh` after deploying this version.

`--no-build` requires the selected image tag to exist locally and refuses a missing tag before
backup or migration. The completion message offers rollback only when `:previous` exists.

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
After web health checks, deploy inspects the actual Docker gateways and Caddy addresses (IPv4
and IPv6), plus the configured bind address, and runs `python -m repeater.cli check-proxy` in
the web container. An uncovered source address fails deployment explicitly. Add only the
observed proxy address or its intended subnet to `REPEATER_FORWARDED_ALLOW_IPS` and redeploy;
the script never broadens trust automatically.

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

## Customer operations (2026-10-16)

See [the operator runbook](../docs/ar/13-customer-operations.md) for the unified calendar,
manual bank-transfer review, quotas, access closure, Hostinger setup and full recovery sequence.
`configure_mail.py` validates an owner-entered mailbox password without sending mail, then writes
protected SMTP settings atomically. SMTP configuration alone does not prove message delivery.

`recovery_drill.py --receipt /srv/repeater/backups/status/recovery-drill.json` downloads the configured
offsite DB/media, restores into a disposable PostgreSQL container with no network and no worker,
and verifies referenced files before cleaning up. It never restores over production. Its public
status marker holds only an epoch; the detailed receipt is mode 600. Run after backup changes and
at least weekly during the pilot. Maintain an independent protected copy of original environment
and encryption keys: a database/media backup cannot replace lost decryption keys.

`.github/workflows/external-monitor.yml` checks public HTTPS from GitHub every ten minutes on the
repository default branch (schedules can be delayed by GitHub). Optional independent SMTP secrets
permit outage/recovery emails; missing secrets mean email delivery is unconfigured. Review Actions
results and account notification settings. Host-side monitoring remains independent of the worker.
No monitoring or recovery check calls a platform publication API.
