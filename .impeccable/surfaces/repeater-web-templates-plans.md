---
version: 1
slug: repeater-web-templates-plans
primary_target: repeater/web/templates/plans/edit.html
related_targets: [repeater/web/templates/plans/index.html, repeater/web/templates/plans/preview.html, repeater/web/templates/plans/execution.html, repeater/web/templates/plans/platform_sources.html, repeater/web/templates/library.html, repeater/web/templates/library_detail.html, repeater/web/templates/sources.html, repeater/web/plan_i18n.py, repeater/web/media_i18n.py, repeater/web/static/app.css, repeater/web/static/app.js]
---

# People and content plans

Mode: Operate. Ordinary extension of the user-pinned Sentinel Eye / Magic Lap system.
No new visual world or generated comp. Preserve DESIGN.md and the shared base contract.

Current state, 2026-10-16: the person library supports mixed content, and explicit owner-only
plan execution and rich publishing routes are implemented behind the server publishing lock.
The execution section at the end of this brief supersedes the earlier execution-deferred and
preview-only capability statements. The preview itself remains an independent simulation.
The phase notes below are historical evidence, not the current capability boundary. PRODUCT.md's
"Person-plan execution and rich publishing" section owns current product truth; production
publishing remains disabled and no live publication acceptance is claimed.

## Phase-one foundation (2026-10-15)

Original phase-one request, 2026-10-15: account linking, folder and page selection,
schedule and its preview, with the person/library foundation. Mock tests only, publication
lock remains enabled, same branch, green CI and deploy.

Audience: invited creators with multiple pages, including nontechnical Arabic and English
users. A person/brand groups their owned destinations and a separate video library. One
saved draft per person. Existing accounts remain unassigned until selected.

Task: create a person; connect accounts through platform OAuth and return to the same plan;
select an accessible Drive folder or paste its link; add supported owned platform sources;
choose pages with optional local search;
save local timezone, start date, weekdays, up to five daily times and library order; choose
once-per-page or repeat mode and its minimum interval; inspect 30 days of simulated content
assignments. Each section saves independently. Folder importing
is a separate explicit action and never accompanies saving or previewing.

Composition: shared rail and utility chrome; person name, preview/library actions; wrapping
step links; sequential task sections using incumbent controls. Targets use a bounded scroll
list of native checkboxes. The preview shows real content or explicit empty slots, with native
disclosures for each time slot's destination assignments and ten time slots per pagination page.
The simulation's totals cover the whole 30-day window.

Quality bar: no fake ready/published states; no cross-person content; server-side native forms;
visible labels/focus; usable Arabic RTL and English LTR, light/dark and mobile. Developer app
approvals, platform publishing settings and deferred TikTok remain honest launch limitations.
Video-only Drive and supported Instagram/Facebook sources feed the person's library and rotation
simulation. All-media support and live or background rotation execution remain deferred.

Phase-one evidence: synthetic local fixture only, no live account actions. Accepted finish evidence is
the viewport-only JPEG set under /Users/mac/dev/repeter-preview/:

- plans-setup-desktop-viewport.jpg
- plans-targets-desktop-viewport.jpg
- plans-schedule-desktop-viewport.jpg
- plans-targets-mobile-viewport.jpg
- plans-schedule-mobile-viewport.jpg
- plans-times-mobile-viewport.jpg
- plans-import-mobile-viewport.jpg
- plans-preview-mobile-viewport.jpg
- plans-preview-en-dark-viewport.jpg

The earlier full-page PNGs contain a capture artifact and are superseded by this evidence.
Existing-font detector warning is waived by the user's pinned Inter/Plex identity; other CSS
advisories concern unchanged legacy styles. Backend tests separately enforce draft and
publishing safety.

Independent phase-one finish verdict, 2026-10-15: SHIP. The sole material finding was checkbox rows
inheriting the shared label's column direction. RESOLVED: page and weekday labels explicitly
use a horizontal flex direction, keeping their native controls beside the label text. No open
finish findings remain. This verdict covers the reviewed interface; the preview remains a
simulation and never publishes, and folder importing remains a separate explicit action.

Documentation records native search/date support and horizontal checkbox-label geometry in
DESIGN.md while preserving the incumbent world. The plan composition, bounded lists, and
preview pagination remain surface-specific. No detector rerun was part of this documentation pass.

## Phase two: rotation preview (2026-10-15)

Direction contract: Operate within the existing Sentinel Eye / Magic Lap family. Preserve the
logical-start rail, utility chrome, semantic tokens, Inter and IBM Plex Sans Arabic, and native
forms. Keep person ownership, draft status, and unfilled slots explicit, with Arabic/English parity.

The rotation section saves independently with a native once/repeat select and a required numeric
minimum interval of 1–365 days (default 30). Each day means 24 elapsed hours. Library order remains
a separate setting and breaks ordering ties; existing plans stay once per page until changed.
The people index and preview show the saved mode, and repeat previews also show the interval.

The 30-day simulation reads the person's current ready library and recorded history when opened.
Each selected page has its own least-recently-used sequence: unseen videos first, then the oldest
recorded or simulated use. Newly ready videos enter on recalculation. Later draft slots assume
earlier simulated slots succeed. First-use and repeat labels accompany assignments, with distinct
wording for the last recorded publication and a previous planned use, using plan-local timestamps.

Insufficient eligible content leaves explicit gaps. Cooldown gaps show the earliest eligibility
time when available; the interval is never shortened to fill a slot. Pending, retrying, and
uncertain or possibly-live publications hold the affected video/page pair until resolved.
The preview explains these holds and reports whole-window repeated and waiting slot counts.
Ownership or connection problems also remain visible as an actionable gap reason.

The existing native slot disclosures and pagination carry the extra information. The only new
style, `.plan-entry-content`, is a vertical text stack with a gap (4px) and maximum inline size
(65ch), grouping the content link or gap reason with supporting usage/eligibility text. It uses
existing typography and colors. This composition remains surface-specific; it is not a shared
component or a new design token.

Saving and previewing remain preview-only: no publication jobs, background scheduler, provider
calls, or publication records are created. Folder importing remains a separate explicit action.
The global publishing lock and independent person-plan publishing guards remain in place.
All review data is a labeled, mock-only local fixture, not live customer activity.

Implemented surface sources: `repeater/web/templates/plans/edit.html`, `preview.html`, and
`index.html`; `repeater/web/plan_i18n.py`; and `repeater/web/static/app.css`.

Accepted phase-two screenshot evidence under `/Users/mac/dev/repeter-preview/`:

- `rotation-setup-en-dark-desktop.jpg`
- `rotation-setup-ar-desktop.jpg`
- `rotation-setup-ar-mobile.jpg`
- `rotation-preview-ar-desktop.jpg`
- `rotation-preview-ar-mobile.jpg`
- `rotation-repeat-ar-desktop.jpg`
- `rotation-repeat-ar-mobile.jpg`
- `rotation-cooldown-ar-mobile.jpg`

Reviewed desktop width was 1571 CSS px and mobile width was 354 CSS px, with no horizontal
overflow. English preview/mobile and pending/actual-history states were source-reviewed without
dedicated captures; the screenshot evidence does not establish visual coverage of those states.

Independent phase-two finish reviewer disposition: SHIP. No material fixes were requested.

| Review criterion | Verdict |
| --- | --- |
| Visual fidelity | Pass |
| Desktop/mobile readability | Pass |
| Arabic/English copy parity | Pass |
| Explicit draft status and gaps | Pass |
| Open findings | None |

The Inter hook warning remains intentional under the user's pinned identity; no suppression was
requested. Documentation adds native number fields to the existing shared-field description and
sidecar; rotation behavior and the new text stack stay in this surface brief. This handoff validates
only the changed design records and does not rerun the detector or browser review.

## Phase three: connected platform sources (2026-10-15)

Direction contract: Operate within the existing Sentinel Eye / Magic Lap family. Extend the
logical-start rail, utility chrome, semantic tokens, Inter and IBM Plex Sans Arabic, and native
forms. Preserve Arabic/English parity, both themes, explicit ownership, and the draft boundary.

The person setup includes a platform-source section between the Drive folder and destinations,
with a matching local navigation link. A person can save multiple owned Instagram professional
accounts and Facebook Pages alongside the existing Drive folder. Native selects choose the
account and either up to the latest 30 videos followed by new videos, or new videos only from
the explicit monitoring start. Saving persistently assigns the account to the person and leaves
monitoring paused. Source account and import mode are fixed after saving; no editing mode is
currently provided. Accounts assigned to another person are unavailable for selection.

Each saved source is a flat list record with its name, platform, readable active/paused status,
chosen import mode, imported/ready/preparing/failed/duplicate totals, and plan-local last-check
and last-success times. Errors use the existing error notice. Start/resume and pause are explicit
form actions; active sources also offer Check now. Monitoring checks for new videos about every
15 minutes. Pausing prevents new checks while queued preparation may finish; resuming preserves
the library and does not repeat the initial archive import. Three consecutive failed checks pause
monitoring, with guidance to resolve the error or reconnect before resuming.

Ready videos enter the person's existing rotation simulation when the preview is recalculated;
the source account is excluded for its own videos. The preview summarizes the total saved source
count, including Drive, without presenting it as an active-monitor count. Source-admin links for
person-owned sources return to the matching section of the person's plan. The library remains
person-scoped. Saving, monitoring and previewing do not activate publication or execute a plan.
The global publishing lock and independent person-plan guards remain in place.

Only video/reel files made available by the supported platforms are in scope. Photos, carousel
albums and stories are excluded. Unavailable files and YouTube originals can use Drive; this
flow adds no official YouTube download or unofficial scraping. TikTok remains deferred. Platform
permissions and download restrictions still apply, and live integration has not been verified.

The new `plans/platform_sources.html` partial is included by `plans/edit.html`. Supporting surface
changes are the source-count summary in `plans/preview.html`, plan links in `sources.html`, and
bilingual copy in `plan_i18n.py`. The surface-specific `.plan-import-list` uses block padding
(20px) and the existing soft divider, removed from the last item. It extends the incumbent flat
list grammar without introducing a shared component, token, or new visual world. DESIGN.md and
`.impeccable/design.json` therefore require no change.

Accepted phase-three screenshot evidence under `/Users/mac/dev/repeter-preview/`:

- `sources-ar-desktop.jpg`
- `sources-ar-mobile.jpg`
- `sources-status-ar-mobile.jpg`
- `sources-en-dark-desktop.jpg`
- `sources-en-dark-mobile.jpg`
- `sources-active-en-desktop.jpg`

These captures use synthetic local data and cover paused-save, active-monitoring and error
states across the reviewed Arabic/light and English/dark desktop/mobile views. Mobile width was
354 CSS px with no horizontal overflow. They establish interface behavior and readability, not
live account imports, platform readiness or publication acceptance.

Independent phase-three finish reviewer disposition: SHIP. No material UI findings remain.

| Review criterion | Verdict |
| --- | --- |
| Fidelity to the existing system | Pass |
| Desktop/mobile readability | Pass |
| Arabic/English copy parity | Pass |
| Save and monitoring separation | Pass |
| Scope, exclusions, and recovery guidance | Pass |
| Material UI findings | None |
| Disposition | SHIP |

The existing Inter detector warning remains intentional under the user's pinned font identity;
the new partial's hook check was clean. This documentation handoff records the accepted review
and source evidence without rerunning the detector or browser. All acceptance data and tests are
synthetic or mocked, and publishing remains locked.

## Phase four: mixed person library (implemented 2026-10-16)

Direction contract: Operate within the user-pinned Sentinel Eye / Magic Lap family. The achieved
surface keeps the logical-start rail, utility chrome, semantic colors, Inter and IBM Plex Sans
Arabic, quiet task cards, and native forms and disclosures. This is a local extension of the
existing world; no rebrand, generated comp, or separate quality-bar artifact was needed.

The person's library now has an Add content disclosure for uploaded videos and static images,
plus a separate Write a text post disclosure. The upload form selects one placement: video
reel/short video, video story, image post, or image story. Text is a post. Item details expose
the same editable placement and allowed-platform controls; changing placement retains the item
and its rotation history. Content-format text accompanies status in the library and assignments
in the plan preview. The unassigned legacy library retains its existing video flow.

Static JPEG, PNG and WebP uploads are bounded to 20 MB and 24 megapixels, normalized to
metadata-free JPEG without cropping, and deduplicated within the person. Text posts accept up
to 5,000 characters. Image details preserve the full image with contain sizing on the existing
canvas color. Text details use the existing body-font caption treatment; library thumbnails
show a bounded excerpt with preserved line breaks and wrapping. These surface-specific styles
reuse existing tokens and do not establish a new shared component.

The compatibility section names each destination and explains why the item fits or is excluded.
The preview adds a native Format exclusions disclosure showing up to 100 item/page pairs while
retaining the total count. Ready, compatible items join the same per-page least-recently-used
simulation, saved repeat gap, first/repeat labels, explicit empty slots, and preview pagination.
Neither format changes nor excluded pairs reset or silently shorten that rotation policy.

The displayed pilot scope permits image posts/stories on Facebook and Instagram, text posts on
Facebook, and the existing short-video formats. Instagram Stories require a known Business
account type; unknown and Creator types receive reconnect guidance. Stories require a 9:16
original, with video stories limited to 3–60 seconds and 100 MB. Instagram image posts require
a 4:5–1.91:1 ratio and captions up to 2,200 characters. Copy presents these as conservative pilot
rules, not universal platform maxima. Story captions remain library notes; they are not rendered
overlays. There is no automatic cropping, music, stickers, links, carousel, or text-to-image flow.
YouTube image/text posts and TikTok photos remain unavailable.

The Drive folder section adds an explicit image opt-in. Images enter as posts and can be changed
to stories in item details. The initial limit covers matching videos and images together; enabling
images on an existing source imports matching unseen images at its next check without resetting
the video baseline. Disabling images stops new image discovery without removing existing library
items; already queued preparation may finish. Social-platform sources remain video/reel-only.
The same submitted image-consent state is preserved through folder browse, pagination, and choice,
including a newly checked opt-in and an existing source's newly unchecked opt-out.

Saving and previewing remain preparation and simulation only. Real publishing and person-plan
execution stay locked behind the global switch and independent person/rich-format guards,
including the legacy signed-video endpoint. No live import or publication acceptance is claimed.
PRODUCT.md and `docs/ar/11-mixed-library.md` own the detailed product and operational scope.

Implemented surface sources: `repeater/web/templates/library.html`, `library_detail.html`,
`plans/edit.html`, and `plans/preview.html`; `repeater/web/media_i18n.py` and `plan_i18n.py`; and
`repeater/web/static/app.css`. No new durable global design decision was introduced, so DESIGN.md
and `.impeccable/design.json` remain unchanged.

Accepted phase-four screenshot evidence under `/Users/mac/dev/repeter-preview/`:

- `media-final-library-desktop.jpg`
- `media-final-library-mobile.jpg`
- `media-final-upload-mobile.jpg`
- `media-final-story-mobile.jpg`
- `media-final-story-desktop.jpg`
- `media-final-edit-desktop.jpg`
- `media-final-plan-desktop.jpg`
- `media-final-exclusions-mobile.jpg`
- `media-review-drive-desktop.jpg`
- `media-review-drive-mobile.jpg`

All captures use synthetic local fixtures. The mobile Drive capture does not include the image
checkbox, so its appearance at that viewport remains visually unverified. The evidence establishes
the reviewed interface only, not live integration or platform publishing readiness.

Fresh independent phase-four finish reviewer disposition: SHIP.

| Finding | Final status | Resolution evidence |
| --- | --- | --- |
| F1 — Preserve Drive image consent | Resolved | One `images_selected` state drives the checkbox and folder forms; both mocked browse → next → choose paths preserve new checked and existing unchecked choices. |

Remaining review criteria: Clear. No regressions identified. The final focused verification
reported 78 passing tests after the consent fix. The Inter hook warning is intentional under
the user's pinned font identity; hooks remained active and no standalone detector was run.
This documentation handoff records the accepted review and its limitations without rerunning
the browser, detector, or tests.

### Phase-four entry-copy follow-up (2026-10-16)

The main phase-four implementation (`0335541`) passed CI and was deployed. A final read of
the live `/plans` page then exposed outdated copy saying photos and stories would come later.
The bounded follow-up corrects the English and Arabic plan introduction, scope, general rotation,
and Drive wording to describe the implemented mixed library and preview accurately. Social
imports remain video/reel-only, and the publishing lock remains explicit. This changes copy,
not the phase-four capability or visual-system scope.

Accepted follow-up evidence is `/Users/mac/dev/repeter-preview/media-entry-copy-desktop.jpg`.
The capture offered as mobile evidence was actually 1728 × 934, so it does not establish narrow
viewport coverage. Mobile confirmation of the corrected copy therefore remains limited; the
earlier mobile Drive checkbox limitation also remains in force.

The follow-up verification reported 118 passing tests across mixed media, content plans, and
plan rotation, including two new rendered-page regressions for English and Arabic copy.
Ruff check, format, and diff checks were clean. Fresh independent follow-up disposition: SHIP.

| Finding | Final status | Resolution evidence |
| --- | --- | --- |
| F1 — Preserve Drive image consent | Resolved | Prior mocked browse, pagination, and selection coverage remains unchanged. |
| F2 — Correct outdated mixed-content copy | Resolved | Desktop render accurately describes the mixed library and publishing lock; English/Arabic rendered-page regressions and the 118-test verification pass. |

No open material findings remain. These observations preserve the distinction between reviewed
UI and verified live integrations. This documentation-only follow-up did not rerun the browser,
detector, or tests.

## Plan execution and publishing copy (2026-10-16)

Direction contract: Operate within the existing Sentinel Eye / Magic Lap blue/slate identity.
The execution extension retains the logical-start rail, utility chrome, Inter and IBM Plex Sans
Arabic, native forms, semantic tokens, and responsive Arabic RTL / English LTR layout. It adds
no visual world, shared component, or design token; DESIGN.md and `.impeccable/design.json`
remain unchanged.

The shared `plans/execution.html` partial appears directly after the heading on both preview
and edit pages, before the simulation or setup sections. An incumbent task card groups the
execution heading, plain-language enabled/stopped state, explanation, lock or workspace-pause
message, and owner actions. The people index replaces its unconditional draft badge with the
plan's actual enabled/stopped state. An enabled state describes the saved plan switch; it does
not assert that publishing gates are open or that content has been published.

Only the workspace owner receives Start this plan or Stop this plan controls. Start uses the
native disabled state while the server is locked or the workspace is paused, with
`aria-describedby` pointing to the visible explanation; the server-lock message takes priority
when both apply. The explanation has status semantics. Stop remains available for an enabled
plan even while either gate is closed. The native POST form carries CSRF and plan revision
values. Other members see the owner-only explanation. The owner action row also links directly
to `/schedule` as Open the actual queue. Existing action wrapping and card spacing carry the
layout at narrow widths; no new responsive rules were introduced.

The execution copy explains that activation lets the worker reserve upcoming eligible slots
from the person's library and that confirmed publications control repeat eligibility. The
adjacent 30-day preview remains a fresh simulation: opening it never starts the plan, consumes
content, or commits reservations. Its limits copy directs users to the actual queue and explains
that execution also checks destination settings, minimum gaps, pending work, and upload quotas.
Editing selected pages, times, or rotation stops the plan for review; person/source changes
invalidate untouched reservations. The stop confirmation says untouched reservations were
canceled and already sent content may be live. These states preserve the distinction between
planned intent, committed work, and uncertain in-flight outcomes.

English and Arabic media copy now says ready, compatible items feed the rotation plan. Format
and platform edits apply to future slots, and saved content can enter an active plan when
publishing is allowed. Text posts identify Facebook Page support. Format scope requires an
enabled plan and the server publishing lock to be lifted; story captions remain library notes
without text overlays, links, music, or stickers. These statements replace the former
simulation-only descriptions without claiming live acceptance of the implemented adapters.

Implemented surface sources are `plans/execution.html`, `plans/edit.html`, `plans/preview.html`,
`plans/index.html`, `repeater/web/plan_i18n.py`, and `repeater/web/media_i18n.py`. Execution routes
and provider adapters exist, but production remains `REPEATER_PUBLISHING_ENABLED=false`; no
plan, source, or rule was activated for this release. Publication verification uses synthetic
accounts and mocked requests only. Platform review, permissions, and live acceptance remain
launch gates. PRODUCT.md owns the detailed execution and supported-format boundaries.

Accepted execution screenshot evidence under `/Users/mac/dev/repeter-preview/`:

- `execution-ar-desktop.jpg` — Arabic preview, 1440 × 1000.
- `execution-ar-mobile.jpg` — Arabic preview, 390 × 844.
- `execution-en-mobile.jpg` — English preview, 390 × 844.
- `execution-edit-en-desktop.jpg` — English edit page, 1440 × 1000.

All captures use synthetic local fixtures and show the publishing-locked state. Active,
workspace-paused, non-owner, error, and index states were source-reviewed without dedicated
captures. The evidence establishes readability of the captured desktop/mobile views, not visual
coverage of every execution state or live platform readiness. Accessibility has source evidence
for native controls, labels, status text, and the disabled-action explanation; keyboard and
assistive-technology interaction were not verified in this review.

Fresh independent execution finish reviewer disposition: SHIP. No material findings remain.

| Review criterion | Verdict |
| --- | --- |
| Identity | Pass |
| Locked explanation | Pass |
| Preview vs queue | Pass |
| Captured desktop/mobile | Pass |
| Bilingual parity | Pass |
| Accessibility | Source pass / interaction unverified |
| Open findings | None |

This documentation handoff records the accepted review and source evidence. It did not rerun
the detector, browser, or tests and does not broaden the reviewed state coverage.


## Customer simplification (2026-10-18)

People rows resume at the next incomplete accounts/content/times step, with owned-page
and ready-item counts. Guided content setup contains the same upload/text forms as the
library and returns to the same person after success or validation failure. Drive and
supported platform imports remain explicit choices. Extra schedule date/zone/order fields
stay available in a disclosure; saved values are submitted without JavaScript. Preview
shows concise totals before its entries, retains diagnostics in disclosures, links directly
to individual slot editing and offers the person's unified calendar as its next action.
Current source and execution restrictions are unchanged. DESIGN.md records the bounded
visual evidence and finish verdict for this simplification.
