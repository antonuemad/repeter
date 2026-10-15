---
version: 1
slug: repeater-web-templates-plans
primary_target: repeater/web/templates/plans/edit.html
related_targets: [repeater/web/templates/plans/index.html, repeater/web/templates/plans/preview.html, repeater/web/templates/library.html, repeater/web/static/app.css, repeater/web/static/app.js]
---

# People and draft content plans

Mode: Operate. Ordinary extension of the user-pinned Sentinel Eye / Magic Lap system.
No new visual world or generated comp. Preserve DESIGN.md and the shared base contract.

User request, 2026-10-15: implement phase one: account linking, folder and page selection,
schedule and its preview, with the person/library foundation. Mock tests only, publication
lock remains enabled, same branch, green CI and deploy.

Audience: invited creators with multiple pages, including nontechnical Arabic and English
users. A person/brand groups their owned destinations and a separate video library. One
saved draft per person in this phase. Existing accounts remain unassigned until selected.

Task: create a person; connect accounts through platform OAuth and return to the same plan;
select an accessible Drive folder or paste its link; choose pages with optional local search;
save local timezone, start date, weekdays, up to five daily times and library order; inspect
30 days of simulated content assignments. Each section saves independently. Folder importing
is a separate explicit action and never accompanies saving or previewing.

Composition: shared rail and utility chrome; person name, preview/library actions; wrapping
step links; sequential task sections using incumbent controls. Targets use a bounded scroll
list of native checkboxes. The preview shows real content or explicit empty slots, with native
disclosures for each time slot's destination assignments and ten time slots per pagination page.
The simulation's totals cover the whole 30-day window.

Quality bar: no fake ready/published states; no cross-person content; server-side native forms;
visible labels/focus; usable Arabic RTL and English LTR, light/dark and mobile. Developer app
approvals, platform publishing settings and deferred TikTok remain honest launch limitations.
Video-only Drive planning is the implemented scope, not all-media or autonomous rotation.

Evidence: synthetic local fixture only, no live account actions. Accepted finish evidence is
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

Independent finish verdict, 2026-10-15: SHIP. The sole material finding was checkbox rows
inheriting the shared label's column direction. RESOLVED: page and weekday labels explicitly
use a horizontal flex direction, keeping their native controls beside the label text. No open
finish findings remain. This verdict covers the reviewed interface; the preview remains a
simulation and never publishes, and folder importing remains a separate explicit action.

Documentation records native search/date support and horizontal checkbox-label geometry in
DESIGN.md while preserving the incumbent world. The plan composition, bounded lists, and
preview pagination remain surface-specific. No detector rerun was part of this documentation pass.
