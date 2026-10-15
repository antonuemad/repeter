---
version: 1
slug: repeater-web-templates-plans
primary_target: repeater/web/templates/plans/edit.html
related_targets: [repeater/web/templates/plans/index.html, repeater/web/templates/plans/preview.html, repeater/web/templates/plans/platform_sources.html, repeater/web/templates/library.html, repeater/web/templates/sources.html, repeater/web/plan_i18n.py, repeater/web/static/app.css, repeater/web/static/app.js]
---

# People and draft content plans

Mode: Operate. Ordinary extension of the user-pinned Sentinel Eye / Magic Lap system.
No new visual world or generated comp. Preserve DESIGN.md and the shared base contract.

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
