---
version: 1
slug: repeater-web-templates-plans
primary_target: repeater/web/templates/plans/edit.html
related_targets: [repeater/web/templates/plans/index.html, repeater/web/templates/plans/preview.html, repeater/web/templates/library.html, repeater/web/plan_i18n.py, repeater/web/static/app.css, repeater/web/static/app.js]
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
select an accessible Drive folder or paste its link; choose pages with optional local search;
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
Video-only Drive planning and rotation simulation are the implemented scope. All-media support
and live or background rotation remain deferred.

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
