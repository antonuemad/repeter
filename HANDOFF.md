# Handoff: Repeater (for Codex)

Written 2026-10-14 from a long Claude Code session. Read this first, then `PLAN.md` (Arabic product and technical plan, the source of truth for intent), then `docs/review/2026-10-14-findings.md` (what the last review found and how it was resolved).

## 1. What this project is

**Repeater** is a multi-tenant SaaS that watches a creator's *source* platform (Instagram, Facebook Page, a Google Drive folder, manual uploads, or an opt-in yt-dlp path for TikTok/YouTube), stores each new short video once, encodes one file that all four platforms accept, and re-publishes it to **TikTok, YouTube Shorts, Instagram Reels and Facebook Reels** on a rotation schedule. It uses the official APIs only for publishing.

- Owner/operator: Antonius (antonius@twemix.com), company **Magic Lab / Magic Lap**. The user writes Egyptian Arabic; keep user-facing replies in Arabic, code and docs in English unless a file is already Arabic (`PLAN.md`, `docs/ar/`).
- First deployment (phase 0): Magic Lap's own accounts at **https://rotator.magic-lap.com** on an existing Hostinger **KVM 4 VPS** that may also run other Magic Lap projects. The main site `magic-lap.com` is on Hostinger web hosting (hPanel) behind Cloudflare and must not be touched. The `rotator` subdomain does not exist yet.
- Repo: `antonuemad/repeter`. Develop on branch `claude/saas-video-platform-plan-tfi4ar`. **No pull request has been opened**; do not open one unless the user asks.

## 2. State at handoff

| Item | State |
|---|---|
| Code | Phase 0 implemented: about 19.5k lines of source and 24k lines of tests |
| Tests | 1657 passed, 10 skipped (shellcheck/rclone variants) on the integration run; `ruff check` and `ruff format --check` clean; `alembic upgrade head` + `alembic check` clean |
| Docker | Image builds; compose stack smoke-tested (db, migrate, web, worker); `deploy/selftest.sh` passes as a non-root user |
| CI | GitHub Actions `ci.yml`. The last run before the final fix failed only the "Docker image and deploy files" job (selftest ran as non-root and the status dir was created by Docker as root). Fixed in `deploy/selftest.sh` and `deploy/deploy.sh`; confirm CI is green on the newest commit |
| Review | 86 confirmed findings fixed by six owners and re-verified by independent skeptics: **81 fully fixed, 5 partial**, plus 18 new minor issues the re-verification found (section 7). No critical finding remains; none blocks internal use, five medium ones should be closed before the first deploy |
| Deployed | **Nothing is deployed.** The sandbox that built this had no SSH key, no server address and no credentials |

## 3. Blocked on the user (do these first, they cannot be done from the repo)

1. DNS: create an `A` record `rotator` -> the VPS IP in Cloudflare, **DNS only (grey cloud)**. Reasons are in `docs/ar/00-deploy.md` (100 MB body limit, bot protection blocking Meta/TikTok fetchers, Let's Encrypt).
2. GitHub repository secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_KNOWN_HOSTS` (now mandatory for deploys), `REPEATER_DOMAIN=rotator.magic-lap.com`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`. Platform keys later.
3. Run the read-only workflow **Server inspect** (`.github/workflows/server-inspect.yml`) first: it shows what already serves ports 80/443. `deploy/bootstrap.sh` never takes those ports when another proxy exists; it prints a ready nginx/Traefik/Caddy snippet pointing at `127.0.0.1:8090` instead.
4. Create the developer apps and give the keys (guides with exact URLs and values): `docs/ar/01-tiktok.md`, `02-google.md`, `03-meta.md`, `04-telegram.md`.

The user believed the SSH access was available to the previous agent. It was not. Do not assume you have it; ask.

## 4. Repository map

```
repeater/
  config.py            settings (env prefix REPEATER_); deploy/.env.example must list every field (tests enforce it)
  models.py            all tables; one initial Alembic migration in migrations/versions/0001_initial_schema.py
  db.py queue.py       SQLAlchemy engine; Postgres job queue (FOR UPDATE SKIP LOCKED, dedupe index, leases)
  crypto.py            envelope encryption of OAuth tokens (AES-256-GCM, key ring), signed media URLs
  accounts.py          connected-account service: token refresh, connect, disconnect
  platforms/           base.py (contracts), http.py, tiktok.py, google.py, youtube.py, meta.py, instagram.py, facebook.py
  sources/             importer.py (poll/import/process), drive.py, ytdlp.py, manual.py, jobs.py
  media/               probe.py encode.py filters.py fingerprint.py retention.py storage.py
  engine/              scheduler.py (planner), publisher.py (publish/poll pipeline), actions.py, captions.py, rules.py, jobs.py
  jobs/                worker.py (threads + ticker + heartbeat), maintenance.py, registry.py, kinds.py
  web/                 FastAPI app, auth, CSRF, i18n (ar/en), consent screen, routes/, templates/, static/
  backups.py cli.py    backup status helpers; CLI: migrate, gen-secrets, check, create-admin, rotate-keys
deploy/                docker-compose.yml, Caddyfile, bootstrap.sh, deploy.sh, backup.sh, selftest.sh, README.md
.github/workflows/     ci.yml, deploy.yml, server-inspect.yml
docs/ar/               operator guides (Arabic): deploy, TikTok, Google, Meta, Telegram, operations
docs/research/         official-docs fact sheets (2026-10-14) with source URLs: tiktok, youtube, instagram, facebook, market
docs/review/           2026-10-14 review: every finding, fix status, re-verification verdict
tests/                 pytest; real PostgreSQL per session, real ffmpeg, respx for HTTP
```

**Deviation from PLAN.md:** the plan mentions Celery and Redis. The implementation uses a **Postgres-backed queue** and has no Redis. That was deliberate (one fewer service on a shared VPS). `plan_workspace` runs every 5 minutes per workspace; sources poll on their own interval.

## 5. Run, test, deploy

```bash
uv venv .venv && . .venv/bin/activate && uv pip install -e ".[dev]"
export REPEATER_TEST_PG=postgresql+psycopg://postgres@127.0.0.1:5432/postgres   # user must be able to CREATE DATABASE
python -m pytest -q -p no:cacheprovider        # about 7 minutes; each session creates and drops its own DB
ruff check . && ruff format --check .
python -m repeater.cli gen-secrets             # SECRET_KEY and TOKEN_ENCRYPTION_KEYS
python -m repeater.cli migrate
```

Needs Python 3.12+ (CI uses 3.12; the dev venv used 3.13), ffmpeg, PostgreSQL 16. Production runs `docker compose` from `deploy/` (see `deploy/README.md` and `docs/ar/00-deploy.md`); `deploy.sh` also pulls a fresh yt-dlp daily.

Sandbox-only notes (ignore if your environment differs): Docker had to be started manually (`dockerd &`); images built behind a TLS-intercepting proxy need `--secret id=build_ca,src=/root/.ccr/ca-bundle.crt`; GitHub was reachable only through MCP tools, not `gh`. A Stop hook in that environment required the working tree to be committed and pushed at the end of every turn.

## 6. Design decisions to keep (changing them reopens review findings)

- **Official APIs only for publishing.** Rules are created **disabled**; the user enables each target account explicitly. YouTube made-for-kids has **no default answer** and must be given. TikTok rules stay disabled until the user picks a privacy level from `creator_info` and accepts the Music Usage Confirmation. While `REPEATER_TIKTOK_AUDITED` is false, the form offers only "Only me".
- **Unknown outcome never retries blindly.** Adapters raise `UnknownOutcomeError(state=...)` once bytes may have been delivered (lost final-chunk response, unreadable 2xx). The engine stores `state` in `publish_state` and polls; empty state means needs-attention ("may be live"). Any non-platform exception after the UPLOADING commit also means needs-attention. `PublishRequest.state` carries earlier attempts' state back to the adapter. `RestartPublish` (from `poll`) means the platform confirmed nothing was published.
- **yt-dlp is isolated.** Sources of kind `ytdlp` never use OAuth tokens or the official listers, need an explicit acknowledgement (`unofficial_ack_at`), and are opt-in. Every platform's terms forbid unofficial downloading even for your own content; this is a business risk the operator accepts. The product is framed and built around "your own original library" (uploads, Drive, official Instagram/Facebook import).
- **Delivery to platforms by URL:** the encoded MP4 is served at `/m/<video_id>/<expires>/<hmac>.mp4` (HMAC-signed, expiring, no session, GET/HEAD/Range, no redirects, `noindex`). Instagram and Facebook fetch it; TikTok uses `PULL_FROM_URL` and falls back to chunked `FILE_UPLOAD` only on `url_ownership_unverified`. The TikTok app must verify the URL prefix `https://rotator.magic-lap.com/m/`.
- **Tokens** are envelope-encrypted (per-value data key wrapped by a key ring); never log tokens, upload URLs, session URIs or `publish_state`. `rotate-keys` re-wraps stored values.
- **Retention contract** (implemented in `jobs/maintenance.py` and `engine/jobs.py`, described in the privacy policy, keep them in sync): tokens deleted on disconnect; account record, post history and log entries for a disconnected account deleted after 5 days (`DISCONNECTED_RETENTION`, inside the 7-day promise in the privacy policy); YouTube-derived identifiers refreshed or scrubbed within 30 days (YouTube Developer Policies III.E.4.c); Meta data-deletion requests delete matching accounts, imported videos/files, posts and log entries; originals purged after `REPEATER_ORIGINAL_RETENTION_DAYS`.
- **Duplicates:** `(platform, external_id)` unique, then SHA-256, then a display-normalised perceptual hash plus audio check. A DUPLICATE video makes its original skip that origin platform/account and cancels already scheduled posts there.
- **Encode profile** (`media/encode.py`): 1080x1920, 30 fps, H.264 High, AAC 48 kHz stereo (bitrate from `AUDIO_KBPS`), faststart, no edit lists, HDR tone-mapped to SDR BT.709, silent audio added if missing, timeout scaled by duration (`encode_timeout_*` settings), preset `medium`.
- **Worker:** 2 threads by default; jobs hold a lease (`job_lease_refresh_seconds`); stale jobs are requeued; due publish/poll jobs are claimed ahead of import backfills; on SIGTERM in-flight jobs get `worker_shutdown_grace_s` (compose grace = that + 60 s).
- **Internationalisation:** Arabic first (RTL), English second; every UI string goes through `repeater/web/i18n.py`. Rule problems are machine codes with `rules.PROBLEM_MESSAGES` as the English fallback.

## 7. Known open issues (final re-verification; full trace in `docs/review/2026-10-14-findings.md`)

Each needs a regression test that fails on the current code. The reviewers' reproduction scripts lived in a temporary scratch directory that is gone; the findings file and the descriptions below are the specification.

**Medium (close before the first deploy):**
1. **Meta data-deletion request arriving after the 5-day account purge** deletes nothing yet reports `completed` (`repeater/jobs/maintenance.py` `_delete_disconnected_accounts` with `repeater/engine/jobs.py` `delete_user_data`). Imported videos and activity rows that quote their captions must still be found and deleted by external user id.
2. **TikTok "Publish now" on a needs-attention post** re-polls the dead upload instead of uploading again (`repeater/platforms/tiktok.py`).
3. **Rollback restore prints (and `docs/ar/05-operations.md`, `docs/ar/00-deploy.md` section 8 repeat) a worker start command without `REPEATER_IMAGE_TAG`**, which brings the broken release back (`deploy/backup.sh`). Related: pre-deploy dumps rotate by count, so reruns and failed rollback attempts can evict the only pre-migration dump.
4. **BT.601 SDR sources** (smpte170m / bt470bg) are retagged BT.709 without a matrix conversion, so colours shift (`repeater/media/filters.py`).
5. **`SAR_SCALE` clamps width to 4096 without scaling the height**, distorting any source wider than 4096 px (`repeater/media/filters.py`).

**Partially fixed:**
- `engine-8` / `ux-compliance-missed-2`: the activity row written by `importer._mark_duplicate` quotes a deleted video's caption and carries only `duplicate_of_id`, so it survives `delete_user_data`.
- `api-tiktok-youtube-5`: no exact per-day ledger of YouTube `videos.insert` session starts and no project-wide circuit breaker; attempts-weighting under-counts some cases. The planner uses `youtube_daily_upload_quota` (default 100, budget 90 percent).
- `ux-compliance-8`: activity entries and error strings written by the engine, publisher, importer, platforms and notify carry no message key and show in English in the Arabic UI (also `sources/manual.py` "not a supported video file"). Needs message keys end to end.

**Low:**
- TikTok `PULL_FROM_URL` init answered with HTTP 5xx is retried with a second init and could post twice (`platforms/tiktok.py`).
- Facebook `poll()` can send the "finish" request while the workspace is paused or the rule disabled (`platforms/facebook.py`, `engine/publisher.py` `PUBLISH_ON_POLL`).
- `delete_account_records` matches account names by substring and can delete other accounts' activity rows (`accounts.py`).
- Overview "needs attention" lists every OFF rule with incomplete settings (`web/routes/overview.py`); dark-mode link contrast in warning banners is below AA (`web/static/app.css`); the "no accounts" error mentions `ads_read` as declined (`platforms/meta.py`).
- Originals of ARCHIVED videos that never got an encode are never purged (`media/retention.py`); a PROCESSING original whose job died absorbs later identical uploads as DUPLICATE with nothing to recover them (`sources/importer.py`); `test_source_disabled_during_a_poll_creates_no_videos` does not exercise the in-poll disable path.
- `deploy.sh --no-build` silently builds the current source under a missing tag and prints "Roll back with" even when `:previous` does not exist; the trusted-proxy list is never checked against the real proxy address, so a mismatch silently collapses all clients into one login-throttle bucket (`deploy/docker-compose.yml`).

## 8. Backlog, in priority order

1. Close the medium items in section 7, then the partial and low ones.
2. Deploy phase 0 once the user supplies section 3; run `deploy/selftest.sh` and `server-inspect` first; verify `https://rotator.magic-lap.com/healthz`; create the admin; connect Magic Lap's accounts (Meta Pages and Instagram in Development mode, Google in Testing, TikTok sandbox/private).
3. Soak for 30 days: no manual interventions, failure rate under 5 percent (phase 0 exit criterion in `PLAN.md`). Add uptime monitoring (Uptime Kuma or healthchecks.io on `/healthz`) and off-site backups (`RCLONE_REMOTE`).
4. Phase 1, **not started**: self-service sign-up with email verification and password reset, workspace invites and roles, plan limits enforcement, onboarding, landing page (Arabic/English), billing (Paymob for EGP, Paddle as merchant of record for USD; Stripe does not support Egyptian companies), support email.
5. Phase 2, approvals (start early, they take weeks): TikTok app audit for the **multi-creator product**; Google OAuth verification (sensitive scopes, no CASA) and YouTube API compliance audit for quota above 100 uploads/day; Meta Business Verification and App Review (screencasts per permission, data deletion callback already implemented).
6. Legal: Egypt Personal Data Protection Law executive regulations (Decree 816/2025) grace period ends **2026-11-01**; hosting Egyptian users' data on an EU/US VPS is a cross-border transfer needing a permit; a DPO is required. Needs a local lawyer. GDPR representative if EU users are targeted. Have the accountant confirm VAT treatment (Egypt 14 percent; MoR handles KSA/UAE).

## 9. Platform facts and risks you must not forget

Sourced details (dates, URLs) are in `docs/research/*.md`; policies change, re-verify before relying on any number.

- **TikTok:** unaudited apps can post only `SELF_ONLY` and for at most 5 users per 24 h. TikTok's Content Sharing Guidelines list as *not acceptable*: "a utility tool to help upload contents to the account(s) you or your team manages" and "an app that copies arbitrary contents from other platforms to TikTok". Phase 0 for Magic Lap's own accounts will therefore stay private, and the audit should be sought for the multi-creator product. TikTok has no official file download for a creator's own videos (Display API gives metadata and share links only; Data Portability is EEA/UK only).
- **YouTube:** the Data API has no download endpoint and the Developer Policies forbid clients from downloading content. The live `videos.insert` page (checked 2026-10-08) says unverified projects are *not* restricted to private; older notices and third parties said the opposite, so be ready for locked-private uploads. Default quota is 100 `videos.insert` calls per project per day (shared by all tenants). OAuth scopes are sensitive, not restricted. In Testing status refresh tokens die after 7 days.
- **Instagram:** `video_url` must be publicly fetchable; the API publishing quota is documented as both 50 and 100 per 24 h (read `content_publishing_limit` at runtime); `media_url` is omitted for videos with licensed music, so some reels cannot be imported. There is no webhook for the owner's new posts, so polling is required.
- **Facebook:** Reels via `video_reels` (3 to 90 s in the docs, though Meta announced no length limit in June 2025), 30 API reels per Page per 24 h, Page tokens never expire. `ads_read` is requested because Business-portfolio roles need it for Instagram publishing (verified against the docs; keep the comment in `meta.py`).

## 10. Working conventions in this repo

- Tests first: every bug fix gets a regression test that fails on the old code. Never skip or weaken a test to get green.
- Keep `deploy/.env.example` in sync with `repeater/config.py`; keep `PLAN.md`, the privacy policy text and the retention jobs consistent.
- No secrets in logs, exceptions, the activity log, CI output or commits. `deploy/.env` is chmod 600 and git-ignored.
- Schema changes: edit `models.py` and amend the single initial migration only while nothing is deployed; after the first deploy, add real Alembic revisions.
- Commit trailers from earlier commits (`Co-Authored-By: Claude ...`, `Claude-Session: ...`) were added by the previous tool; you do not need to keep them.
- Do not push to branches other than the one above without the user's say-so, and do not open a PR unprompted.
