# Customer operations review — 2026-10-16

Scope: weekly calendar/slot overrides; quotas and bank-transfer invoice review; member roles,
closure and session invalidation; encrypted transactional email; host/offsite monitoring and
isolated DB/media recovery verification. Existing platforms only; publication remains locked.
SMTP Hostinger mailbox credentials and commercial prices/bank details are unconfigured.

Independent visual finish review: **ship**. The first fix disposition was resolved in one batch.

| Fix | Final score |
| --- | --- |
| F1 shared native fields, including datetime-local/untyped | resolved |
| F2 readable stacked mobile activity | resolved |
| F3 service health queue/backup/drill/external-monitor evidence | resolved |
| F4 current product/design/surface documentation | resolved |

Remaining: clear; no material regressions found in supplied recaptures. This verdict approves
reviewed UI only and establishes neither live delivery nor publishing readiness.

Evidence: /Users/mac/dev/repeter-preview/operations-ui contains first desktop1728/mobile390
AR/light and EN/dark customer-route screenshots, and final edit/setup/activity/service-health,
customer administration, expanded catalog, invoice, member-role and closure recaptures.
All preview identities, pages, posts and invoice references are synthetic; no payment or
platform publication was submitted. Document width matched viewport on all main routes.
Inter is user-pinned and the inherited side-tab detector finding describes native local
settings navigation; neither calls for replacing the approved Magic Lap system.

Verification: new PostgreSQL tests cover bilingual rendering, isolated tenant access,
slot move/replacement/exclusion and history, stale/CSRF guards, actual reservation cancellation,
encrypted mail/revocation/retry/suspended recovery, quotas including concurrent creation,
manual invoice review/replay and access lifecycle. Host tests cover alert state and verified
TLS, mailbox setup writes only after successful authentication, and media validation rejects
missing/tampered/unsafe files. A 20-tenant, 60-person, 120-account, 1200-item, eight-thread test
performs60 local rotation previews without any provider request. This measures local preview
work, not publishing throughput. On macOS the broad run retains an existing ffmpeg voiceover
fingerprint failure; Linux CI is the complete release gate.

The real offsite dump repeater-db-20261016T031701Z-Bb9D6I.sql.gz restored successfully into a
throwaway `--network none` database in116.42seconds: one workspace, zero library items and
media files. No production DB change, application or worker startup. Customer-media recovery
cannot be claimed before such media exists in a tested backup.
