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

The phase notes record the delivery sequence. The execution update below supersedes earlier
preview-only limitations; the testing lock remains mandatory.

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

## Person-plan execution and rich publishing (2026-10-16)

Plans now have explicit owner-only activation/pause and start disabled, including migrated plans.
Activation requires the server publishing switch, an unpaused workspace and selected destinations.
The periodic worker reserves eligible upcoming slots within the workspace horizon through the
same durable publish queue. The plan's local times replace legacy rotation windows; legacy
rules still supply platform settings and minimum gaps. Assigned pages never run both planners.
One pending item/page pair is allowed; confirmed publication time controls repeats. Preview
remains a fresh 30-day simulation, not the committed queue; a direct link opens actual reservations.
Editing pages, times or rotation stops the plan for review. Pause cancels only untouched work;
already sent content may be live. In-flight outcomes retain their state and cannot be resent blindly.

Facebook Page adapters cover text posts, image posts and photo/video Stories. Instagram professional
accounts support image posts; known Business accounts support photo/video Stories. Existing reels/
short video adapters remain available. Story descriptions stay library notes, without overlays.
These routes reuse the format compatibility rules and are verified with denied-unmocked-network tests.
They have not been accepted against live platform accounts. Platform review, permissions and account
restrictions remain launch gates. TikTok photos, YouTube image/text posts and carousel albums remain out.

Migration 0007 adds disabled-by-default execution and unique plan/revision/page/slot reservations.
Retries retain their original slot, pauses/edits invalidate untouched reservations, and worker replay
cannot publish a completed slot again. YouTube reservations use the shared project quota lock/ledger.
The worker rechecks ownership, eligibility, actual repeat history and all execution gates before a
fresh upload. Rich signed-media URLs require publishing enabled; authenticated library previews work
while locked. Production remains `REPEATER_PUBLISHING_ENABLED=false`, with no plan/rule/source activated
and no real platform publication test. Enabling the server switch is expressly outside this release.

## Invited-customer journey review (2026-10-16)

The overview now guides the newest person's preparation: create the person, connect and save their
selected pages, and add ready content to their own library. It names the person and links to that
plan's preview when all three preparation steps are complete. Readiness does not certify platform
compatibility or enable publishing; the preview explains exclusions and empty slots. Unassigned
content, other people's libraries, processing files and unselected accounts do not complete these steps.

Person libraries link directly to their preview and setup. Drive is explicitly optional. Authorized
person uploads and text creation retain the person and bounded title/caption drafts on validation
errors; files must be selected again. Item edits also retain text, format and platform choices after
validation errors. These drafts are rendered in the response, not stored in session cookies.
Existing plans can change their times/days while keeping a start date that has already passed.
Invitations default the password setup page to the invited workspace's language, preserving an
explicit browser language choice. All verification is synthetic; the production publishing lock remains.

## Guided customer setup (2026-10-16)

`/setup` is the primary first-person entry. After naming a person, one four-step wizard connects
and selects their accounts/pages, selects optional Drive content, saves local scheduling and
rotation together, and previews 30 days. OAuth success/cancellation returns to the originating
accounts or content step; only owned, allowlisted return paths are accepted. The existing advanced
editor stays available. Each explicit save persists progress; unsaved form edits are not autosaved.
New wizard-created plans default to repeat mode with a 30-day minimum; existing plans keep their mode.

Folder browsing, access checks and import controls remain separate from publishing. Saving a folder
never starts its monitor; starting imports is explicit, with ready/preparing/failed library counts.
Manual upload/text creation remain available with direct links back from the library and item detail.
Schedule and repeat settings save atomically and stop any active plan for review, as existing saves do.
The final step uses the same read-only simulation and explains empty slots, incompatibilities and
repeat waits. No activation control is included in the wizard, and production publishing stays locked.
All verification uses synthetic data, fake OAuth and mocked provider HTTP; external provider approval
and first-customer consent/import acceptance are still required independently.
