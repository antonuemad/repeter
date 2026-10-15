# Repeater

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The first trial is for selected customers invited by Magic Lap (confirmed by the
user, 2026-10-15). Creator and operator workflows already exist in PLAN.md and the
application. The platform administrator creates separate paused customer workspaces and issues
private invitation links. Customers set their own passwords and explicitly accept
the current legal texts. During this pilot the administrator copies and delivers
links manually; automatic email delivery is not configured.

## Product Purpose

Keep a private short-video library and distribute the creator's own content to
connected accounts under explicit publishing rules. Success means an operator can
connect the correct account, import or upload content, review its settings and
schedule, and understand failures without causing duplicate posts.

## Operating Context

FastAPI/Jinja web application, PostgreSQL, an encoding and publishing worker, and
a single-host Docker deployment at https://repeater.magic-lap.com. Arabic and
English are supported, with workspace-local time. Source and publishing APIs have
different capabilities; not every platform is an official download source.

## Capabilities and Constraints

- Manual uploads, Google Drive, supported connected-platform sources and explicitly
  opted-in unofficial sources feed the library.
- Rotation rules, publication history, attention states, pause and reconnect are
  existing capabilities and must remain available.
- Google and Meta developer credentials and initial account links (including read-only Drive)
  are configured (2026-10-15). TikTok setup was explicitly deferred after signup email delivery
  failed. Customer platform approvals and real publication acceptance
  remain launch gates; a healthy server does not establish publishing readiness.
- Preserve owner/editor authorization, tenant isolation, CSRF, explicit consent,
  platform-required settings and unknown-outcome publication safeguards.
- User restriction, 2026-10-15: no real publication to any page or account during testing,
  including private/unlisted test posts. Keep publishing disabled on the server until
  the user explicitly ends the restriction; publication tests use mocks and synthetic accounts.
- The installed database migration is immutable. Any later schema change needs
  a new Alembic revision.

## Brand Commitments

The user explicitly requires the approved Magic Lap UI/UX represented by
https://sentinel-eye.magic-lap.com/. Repeater keeps its own product name and content.
The old purple horizontal navigation is being replaced.

## Evidence on Hand

PLAN.md, HANDOFF.md, docs/review/2026-10-14-findings.md, tests/, current deployment
health checks, and the authenticated Sentinel Eye reference. Local preview data is
synthetic and must never be represented as live customer activity. No customer
results or publication performance claims have been supplied.

## Product Principles

- Show the next useful action and the real state of the workspace.
- Keep publishing decisions explicit and recover uncertain results carefully.
- Give Arabic and English equal navigation and form usability.
- Distinguish implemented functionality from verified live integrations.

## Open Decisions

Manual invitation/recovery link delivery is confirmed (2026-10-15). Existing registered
email addresses cannot be invited into a second workspace in this initial flow.
Full member removal, workspace closure, commercial packaging, support commitments
and trial volume remain open. The review should identify
their impact without inventing pricing, customers or operating guarantees.

## Person-led phase one (2026-10-15)

The primary preparation flow is now People & plans: one person/brand, a separate library,
one Drive folder, selected owned pages, local weekdays/times, and a 30-day draft preview.
Customers connect through the application's OAuth clients; they do not create developer
apps themselves. Google/Meta review and testing-mode limits still apply to customer access.

Plans save intent only. They never create publishing jobs, and assigned pages/person media
are excluded from the legacy publishing paths even if the server switch changes. Folder
importing is explicitly started separately. Each person currently has one draft and one
folder; page ownership stays attached when a page is removed from the draft. Imported video
identity and duplicate detection are scoped to the person's library. Legacy unassigned
content remains available as an explicitly labeled library.

Phase one does not activate publishing, import the latest 30 videos from social platforms,
add photos/stories, or provide automatic rotation. TikTok setup remains deferred. All
acceptance tests for this phase use synthetic accounts and mock platform requests.

## Person-led phase two (2026-10-15)

Saved plans can now rotate their own ready video library in the 30-day draft preview.
Each page has an independent least-recently-used sequence: unseen videos first, then the
oldest recorded or simulated use; library order breaks ties. A repeat waits 1–365 elapsed
24-hour days (default 30). Existing plans retain once-per-page behavior until changed.
Newly ready content is included when the preview is recalculated. Insufficient content
leaves explicit gaps with the earliest eligible time; it never shortens the repeat interval.

Confirmed publication history establishes the last actual use. Pending, retrying and
unresolved/possibly-live publications block that video/page pair until resolved. Preview
rows label first use, repetition, and whether a previous use is actual history or an earlier
simulated slot. Later simulated slots assume earlier draft slots succeed. Opening a preview
does not consume content, record a publication, run a background scheduler or contact a
provider. Saving rotation settings never starts imports or publishing; both the global lock
and independent person-plan publishing guards remain in place. Live execution is still a
separate launch gate, as explicitly required during the customer test period.

## Person-led phase three (2026-10-15)

Each person can now add multiple connected Instagram professional accounts and Facebook
Pages as video sources alongside their Drive folder. Save a source with either latest 30
videos then follow new content, or new content only from the explicit monitoring start.
Saving assigns the source account to that person but leaves monitoring paused. Start/resume,
pause and check-now are separate actions; periodic discovery runs about every 15 minutes.
Source status, last check/success, preparation failures and ready/duplicate counts are visible.
Already queued file preparation may finish after pausing. Ready content automatically enters
the next rotation preview; its original source account remains excluded for that video.

Only available video/reel files are supported, with the existing platform permission and
copyright/download restrictions. No photos, stories or carousel albums. YouTube originals
can come through Drive; unofficial scraping is not added to the person flow. TikTok remains
deferred. This phase's verification uses mock APIs and synthetic media only. Production
sources are not started by deployment; publishing and execution of person plans remain locked.

## Person-led phase four (2026-10-15)

Person libraries now accept uploaded videos, static images and text posts. Each item has one
editable placement: reel/short video or story for videos, post or story for images, and post
for text. Changing placement keeps the same item and rotation history. Optional JPEG/PNG/WebP
imports extend the person's Drive folder; social-platform sources remain video/reel-only.
Images are validated, bounded to 20 MB/24 megapixels, normalized to metadata-free JPEG without
cropping and deduplicated within the person. Text posts are limited to 5,000 characters.

The mixed library feeds the same per-page rotation simulation and repeat gap. Preview capability
rules permit image posts/stories on Facebook and Instagram, text on Facebook, and existing short
videos. Instagram Stories require a known Business account type captured when reconnecting;
unknown/Creator account types are excluded. Pilot stories require 9:16 media, with video stories
limited to 3–60 seconds and 100 MB. Instagram image posts require a 4:5–1.91:1 ratio and captions
up to 2,200 characters. These conservative pilot rules do not claim universal platform maxima.

Compatibility and exclusions appear in item details and schedule previews. Story captions are
library notes, not rendered overlays; no automatic cropping, stickers, music, links, carousel
albums or text-to-image generation is included. YouTube image/text posts and TikTok photos are
not enabled. Rich-content provider publishing adapters and execution of person plans remain a
future gate; this phase implements preparation and simulation only. Both the global publishing
lock and independent rich-format/person guards stay in force, including the legacy signed-video
endpoint. No live import or publication acceptance is performed. Migration 0006 preserves existing
video defaults; rollback archives rich formats before removing their type columns.
