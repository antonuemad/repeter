# Handoff: Repeater (for Codex)

Written 2026-10-14 from a long Claude Code session. Read this first, then `PLAN.md` (Arabic product and technical plan, the source of truth for intent), then `docs/review/2026-10-14-findings.md` (what the last review found and how it was resolved).

## Latest update: platform links and testing restriction (2026-10-15)

The user forbids all real publication to every page/account throughout testing, including
private/unlisted test posts. See `AGENTS.md`. Production must keep
`REPEATER_PUBLISHING_ENABLED=false`; manual publish, planning, queued publication and publisher
polling are blocked independently of workspace/rule settings. Never lift the lock or run a real
publication acceptance test until the user explicitly ends the restriction. Use mocks only.

Google/Meta initial account links succeeded: 1 YouTube, 95 Facebook Pages, 51 Instagram accounts,
and 1 read-only Google Drive account. Drive folder import has been exercised with mocked HTTP
and synthetic media only; no customer folder or live media was imported as an acceptance test.
All 147 rules are disabled and the current workspace is paused. TikTok is not configured.
The user explicitly deferred TikTok developer setup after the signup email code failed to arrive.
Do not repeatedly request signup or invent credentials. Telegram delivery is also deferred.
Encrypted Drive offsite backups and an isolated database restore have been verified; media
restoration awaits real media. Host monitoring runs; Telegram delivery is deferred by the user.
See `docs/ar/07-pilot-operations.md` for operator checks. No live publication has been performed.
`integration-status --testing --platform gdrive --platform youtube --platform instagram --platform facebook`
checks the selected links, disabled publication and all three fresh backup receipts without API
calls or alerts. The default report still includes TikTok so that deferral is visible. It is not
customer platform approval or live publication certification.

## Earlier update: manual invited-customer access (2026-10-15)

The invitation/password setup/manual recovery lifecycle is implemented. Read
[the operator guide](docs/ar/07-invitations.md) and
[the fresh access review](docs/review/2026-10-15-invited-access.md).
Settings now lives at `/settings/workspace`, with members at `/settings/members`.
Platform operators use `/admin/customers` and `/admin/recovery`; workspace ownership
never implies platform administration. `grant-operator --email` explicitly promotes
an existing authorized operator without resetting its password. `create-admin`
retains owner-only semantics. New migration **0002**, never edit deployed 0001.

User confirmed **manual copied links**, no automatic email. Invites expire after
72 hours; reset links after 30 minutes; both are one-use. Customer workspaces start
paused. Existing registered email addresses cannot be invited into another workspace
in this initial flow. Recovery requires verified identity plus operator password;
operator account recovery uses the trusted CLI. Stored legal consent is now always
authoritative; legal version 2026-10-15 requires users to accept the updated policy
for themselves. Never accept it in the production browser on their behalf.

The account-access gap is closed for the managed pilot. Real platform setup and
publication, offsite recovery and alerts remain open gates; full member removal,
workspace closure and account session management remain later lifecycle work.
Deployment must follow green CI on the exact SHA, a pre-migration backup, explicit
operator grant for the existing production owner, and health/revision verification.
No synthetic customer or publishing data belongs in production.

Pre-push validation: 425 broad web/message/core/migration/CLI checks passed,
then 71 final access/migration/CLI checks passed after the last concurrency fixes.
Ruff and diff checks clean. Independent access UI review: **ship**, no material
fixes, with desktop AR/mobile EN and public password/recovery captures. Source-only
coverage for issued-link/code/error visuals; no automated production password entry
or legal acceptance. The pushed SHA still requires the complete Linux CI jobs.

## Previous update: invitation trial and Magic Lap UI (2026-10-15)

The user selected **a trial for invited customers**, and explicitly requested the
approved Sentinel Eye UI/UX. Read [docs/ar/06-readiness.md](docs/ar/06-readiness.md)
for the launch assessment, [PRODUCT.md](PRODUCT.md) for scope, and
[DESIGN.md](DESIGN.md) plus `.impeccable/design.json` for the built visual system.
The UI now uses the Sentinel Eye shell, self-hosted Inter/Plex fonts, explicit
light/dark preference, native mobile navigation, and actionable setup/empty states.
The upload CTA opens its form even when the library already contains videos.

**Customer-trial readiness is still blocked:** no production platform credentials
or accounts; no implemented invitation/password-reset lifecycle; no offsite backup
or configured operator alerts; no real end-to-end platform acceptance evidence.
Do not describe the healthy deployment or simulated tests as live integration proof.
The single initial owner account is not a shared customer account.

Validation: 335 web/message tests passed on local PostgreSQL (334 in the full web
run plus the added upload-action regression, which was red before the fix and green
after it); Ruff clean. Independent UI finish review disposition **ship**, no material
fixes, with optional long-workspace-name truncation on mobile. Desktop/mobile and
Arabic/English/light/dark captures are in `/Users/mac/dev/repeter-preview/` locally;
all preview content is synthetic and the preview has no worker. Production remains
free of demo data. The generic Inter detector warning is retained intentionally to
match the user-pinned reference.

The actual daily production dump was restored successfully to a throwaway
Postgres container with no network/ports, tmpfs storage and no worker. Schema 0001,
1 user, 1 workspace and 0 posts were verified; the container was removed. This does
not establish media recovery, offsite recovery, or capacity/RTO for customer load.
For each release, verify CI on the full commit SHA before deployment and use the
server's `/srv/repeater/app/.deployed-revision` as the actual deployed revision.

## 1. What this project is

**Repeater** is a multi-tenant SaaS that watches a creator's *source* platform (Instagram, Facebook Page, a Google Drive folder, manual uploads, or an opt-in yt-dlp path for TikTok/YouTube), stores each new short video once, encodes one file that all four platforms accept, and re-publishes it to **TikTok, YouTube Shorts, Instagram Reels and Facebook Reels** on a rotation schedule. It uses the official APIs only for publishing.

- Owner/operator: Antonius (antonius@twemix.com), company **Magic Lab / Magic Lap**. The user writes Egyptian Arabic; keep user-facing replies in Arabic, code and docs in English unless a file is already Arabic (`PLAN.md`, `docs/ar/`).
- First deployment (phase 0): Magic Lap's own accounts at **https://repeater.magic-lap.com** on an existing Hostinger **KVM 4 VPS** that may also run other Magic Lap projects. The main site `magic-lap.com` is on Hostinger web hosting (hPanel) behind Cloudflare and must not be touched. The requested `repeater` subdomain is now deployed (2026-10-15, Cairo time).
- Repo: `antonuemad/repeter`. Develop on branch `claude/saas-video-platform-plan-tfi4ar`. **No pull request has been opened**; do not open one unless the user asks.

## 2. State at handoff

| Item | State |
|---|---|
| Code | Phase 0 implemented: about 19.5k lines of source and 24k lines of tests |
| Tests | Follow-up Linux run: **1717 passed, 8 skipped** (Docker CLI checks unavailable inside the disposable test container); `ruff check`, `ruff format --check`, `bash -n` and `shellcheck -x` clean. Alembic upgrade/check, downgrade to base, then upgrade/check again all clean. |
| Docker | Image builds; compose stack smoke-tested (db, migrate, web, worker); `deploy/selftest.sh` passes as a non-root user |
| CI | GitHub Actions `ci.yml` verifies lint/tests and the Docker image/deploy self-test. The previously deployed `f30676da58fb916c0d6007b08162c14a57a13e27` was green; verify the full SHA of each newer release in Actions before deploying. |
| Review | 86 confirmed findings fixed by six owners and re-verified by independent skeptics: **81 fully fixed, 5 partial** at the original independent review snapshot. The follow-ups in section 7 resolve the remaining original partial findings and the subsequently reported medium/low issues; regression coverage verifies the changes. No independent re-review is claimed |
| Deployed | **Phase 0 is live at https://repeater.magic-lap.com.** Existing local SSH alias `magic-lap-vps` reaches the VPS as `deploy` with passwordless sudo. App files: `/srv/repeater/app`; nginx forwards only this subdomain to `127.0.0.1:8091` (8090 belongs to the existing WhatsApp project). PostgreSQL/web/worker are healthy. Platform keys/accounts remain unconfigured. |

## 3. Production access and remaining setup

- Deployment used the existing, pinned local SSH alias `magic-lap-vps`; GitHub Actions deployment secrets are currently empty. Do not assume CI can deploy: it only validates code until those secrets are configured. Do not create or broaden SSH keys merely to enable automation.
- Cloudflare now has an A record `repeater` -> the VPS, DNS only. nginx has a separate virtual host at `/etc/nginx/sites-available/repeater.magic-lap.com`. The Let's Encrypt certificate uses the existing ACME account and `/var/www/repeater-acme` webroot. Existing sites and their proxy settings were preserved.
- Admin login details are in a mode-600 local file under `/Users/mac/dev/repeter-deploy/`, outside this repository. Never copy credentials into Git or logs. The owner must accept the app's terms/privacy screen themselves.
- Platform applications/credentials still need setup using docs/ar/01-04; use **https://repeater.magic-lap.com** for all callbacks, privacy and terms links. No real platform accounts were connected during deployment.
- Daily local backup/backup-age cron jobs are installed; off-site rclone and Telegram alerts still need operator credentials/configuration.
- **The production database has now applied revision 0001. From this point on, never amend that initial migration: add new Alembic revisions.**

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
- **Delivery to platforms by URL:** the encoded MP4 is served at `/m/<video_id>/<expires>/<hmac>.mp4` (HMAC-signed, expiring, no session, GET/HEAD/Range, no redirects, `noindex`). Instagram and Facebook fetch it; TikTok uses `PULL_FROM_URL` and falls back to chunked `FILE_UPLOAD` only on `url_ownership_unverified`. The TikTok app must verify the URL prefix `https://repeater.magic-lap.com/m/`.
- **Tokens** are envelope-encrypted (per-value data key wrapped by a key ring); never log tokens, upload URLs, session URIs or `publish_state`. `rotate-keys` re-wraps stored values.
- **Retention contract** (implemented in `jobs/maintenance.py` and `engine/jobs.py`, described in the privacy policy, keep them in sync): tokens deleted on disconnect; account record, post history and owned log entries for a disconnected account deleted after 5 days (`DISCONNECTED_RETENTION`, inside the 7-day promise in the privacy policy); YouTube-derived identifiers refreshed or scrubbed within 30 days (YouTube Developer Policies III.E.4.c); Meta data-deletion requests delete matching accounts, imported videos/files, posts and log entries; originals purged after `REPEATER_ORIGINAL_RETENTION_DAYS`.
- **Duplicates:** `(platform, external_id)` unique, then SHA-256, then a display-normalised perceptual hash plus audio check. A DUPLICATE video makes its original skip that origin platform/account and cancels already scheduled posts there.
- **Encode profile** (`media/encode.py`): 1080x1920, 30 fps, H.264 High, AAC 48 kHz stereo (bitrate from `AUDIO_KBPS`), faststart, no edit lists, HDR tone-mapped to SDR BT.709, silent audio added if missing, timeout scaled by duration (`encode_timeout_*` settings), preset `medium`.
- **Worker:** 2 threads by default; jobs hold a lease (`job_lease_refresh_seconds`); stale jobs are requeued; due publish/poll jobs are claimed ahead of import backfills; on SIGTERM in-flight jobs get `worker_shutdown_grace_s` (compose grace = that + 60 s).
- **Internationalisation:** Arabic first (RTL), English second; UI strings go through `repeater/web/i18n.py`; structured domain messages use `repeater/messages.py` and `message_catalog.py`. Rule problems are machine codes with `rules.PROBLEM_MESSAGES` as the English fallback.

## 7. Follow-up fixes and known open issues ( full trace in `docs/review/2026-10-14-findings.md`)

Each needs a regression test that fails on the current code. The reviewers' reproduction scripts lived in a temporary scratch directory that is gone; the findings file and the descriptions below are the specification.

**Five medium issues resolved in the follow-up:**
1. Late Meta deletion: imports retain `Video.meta_deletion_user_id` when account retention removes their account/source FKs. The deletion job removes these videos/files, their queued imports, and activity entries quoting them through `duplicate_of_id`. Duplicate-release notices now carry that id too. The initial migration was amended because nothing is deployed; privacy text and `PLAN.md` describe the deletion key.
2. TikTok "Publish now": an explicit confirmed retry of a needs-attention post starts a fresh upload. Scheduled/failed retries retain recoverable upload state; the existing may-be-live confirmation remains required in the dashboard.
3. Rollback: the printed worker command carries the selected `REPEATER_IMAGE_TAG`, project paths and stop grace. Pre-deploy dumps now expire by age (30 days), without count eviction by retries; unique filenames preserve dumps taken within the same second. Arabic operator guides match.
4. BT.601 SDR: `smpte170m` / `bt470bg` pixels are matrix/range-converted to BT.709 before output tagging, tested with limited- and full-range colour bars.
5. Wide video: the 4096-pixel intermediate cap scales both axes proportionally, including anamorphic inputs. The shared picture filter also protects fingerprints.

Regression tests are in `tests/test_worker.py`, `tests/test_engine_publish.py`, `tests/test_deploy.py`, and `tests/test_media.py`. See the follow-up section of the review trace for validation.

**Remaining partial and low issues resolved (2026-10-15):**
- YouTube insert accounting now records a durable reservation before every HTTP session-start request, including adapter retries and expired-session restarts. Pacific-day rows serialize concurrent starts across all workspaces and hold a project-wide exhaustion circuit until reset. Post retry/reset/deletion cannot refund spent quota; the planner combines the ledger with pending reservations. A crash immediately before HTTP can conservatively consume one reservation. Before the first deployment, the initial migration was amended to include quota tables and nullable error-message metadata. That migration is now immutable.
- Domain activities and errors carry stable message keys/parameters. Post, video, source and account error metadata survives database reloads, nested messages translate, and every dashboard error surface uses the viewer's language. Actions/manual uploads raise keyed UserError refusals; the old regex is only a compatibility fallback. Telegram follows the workspace locale while preserving the existing default of withholding private details. Legacy unkeyed records retain their original diagnostic; English diagnostics remain available in English.
- TikTok pull-init 5xx is an unknown outcome and cannot trigger another init automatically. Facebook polling is held while publishing is halted because it can send a deferred finish request.
- Account deletion uses structured ownership, preserving unrelated similarly named accounts' logs. Legacy exact-name mentions are redacted; channel refresh also redacts nested translation parameters.
- Disabled incomplete rules no longer appear as overview attention problems. Warning-banner/flash links meet AA in both themes; optional ads_read is excluded from required-permission errors.
- Archived unencoded originals expire. Maintenance fails dead/orphaned import pipelines and releases their dependent duplicates. The source-disable regression now actually disables during the second listing call.
- --no-build refuses missing image tags before migration; first deploys do not advertise an absent rollback image. Deploy validates observed Docker gateway/Caddy/bind addresses against the web container's real FORWARDED_ALLOW_IPS and fails explicitly on mismatch, including custom IPv4/IPv6 ranges.

Regression coverage includes tests/test_followup_regressions.py, tests/test_upload_quota.py,
tests/test_domain_messages.py and the existing platform, engine, source, deployment and web suites.
A local-only preview is available at http://localhost:8765 on this Mac, with fictional accounts,
disabled rules, a paused workspace and no worker. It is not a production deployment. Preview files
and generated media live outside the repository in /Users/mac/dev/repeter-preview.

## 8. Backlog, in priority order

1. The known review partial/low follow-up in section 7 is complete; check the final commit CI before deployment.
2. Phase 0 is deployed and the admin exists. Next connect Magic Lap's accounts after supplying platform app credentials (Meta Pages/Instagram Development mode, Google Testing, TikTok sandbox/private).
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
- Schema changes: production is deployed. Edit `models.py` and add a new Alembic revision for every schema change; do not amend 0001.
- Commit trailers from earlier commits (`Co-Authored-By: Claude ...`, `Claude-Session: ...`) were added by the previous tool; you do not need to keep them.
- Do not push to branches other than the one above without the user's say-so, and do not open a PR unprompted.


## First production deployment notes (2026-10-15)

The application release was cf8aa5b (both CI jobs green). The VPS runs Docker 29, whose text Go
formatter prints an empty netip.Addr IPv6 gateway as `invalid IP`. The deploy guard now reads
JSON-formatted IP fields, omits only empty/null fields, and still rejects untrusted addresses.
Three regressions exercise real CLI validation through the shell guard for IPv4, IPv6 and an
untrusted subnet. Bootstrap resumed successfully after this compatibility fix. Keep future
releases on `claude/saas-video-platform-plan-tfi4ar` unless the owner requests otherwise.
