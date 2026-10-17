---
version: 1
slug: "repeater-web-templates-guide-html"
primary_target: "repeater/web/templates/guide.html"
related_targets: ["repeater/web/guide_content.py","repeater/web/routes/guide.py","repeater/web/static/app.css","repeater/web/static/app.js"]
---

# User guide

## Scope and direction

Mode: **Read**. This surface extends the established Magic Lap / Sentinel Eye application
shell using its existing tokens and fonts. It does not establish a new visual world; global
DESIGN.md and .impeccable/design.json remain unchanged.

The guide provides a complete ordered customer journey in Arabic (RTL) and English (LTR),
with 18 customer chapters and two additional platform-administrator chapters. Ordinary owners
and editors do not receive the administrator chapters or their action links. Content covers
invitation/recovery, navigation, person setup, account selection and management, Drive and
local content, supported-platform imports, the mixed library, rotation, preview, calendar
editing, the testing lock, troubleshooting, advanced tools, workspace settings,
members/security, billing, and administrator customer and operational duties.

## Implemented surface

This layout revision supersedes the previous full-document layout and its review claims. The
user rejected that presentation as unformatted/random using
`/Users/mac/dev/repeter-preview/guide-20261018/guide-live-ar-desktop.jpg`. All guide content is
retained, with exactly one topic rendered per server GET at `/guide?topic=id`.

The centered workspace is bounded to 1120px. Desktop uses a 248px grouped contents rail, a
24px gap and a flexible article surface; article prose is bounded to 72ch. The article uses the
incumbent white surface in the light theme and its surface token in the dark theme. Short
navigation labels keep the rail scannable. The selected topic is highlighted and its group
starts open; native disclosures intentionally allow several groups to be opened manually. The
sticky rail starts at 80px and scrolls within a maximum height of `calc(100vh - 104px)`.

Below 1200px, contents becomes a native disclosure whose summary includes the current topic.
JavaScript initially collapses it and closes it after topic selection. Without JavaScript, the
server-rendered contents remains open and topic navigation still works. Known legacy
`/guide#topic` links migrate through allowlisted topic hrefs already present in the DOM.

Each topic follows one order: heading with position and introduction, numbered steps, note,
one task action, then previous/next navigation across the role-filtered sequence. The
`guide-article` fragment and `tabindex="-1"` belong to the compact H2, so fragment navigation
focuses the heading. Disclosure chevrons use physical right/bottom borders with logical
placement to preserve down/up orientation in RTL.

Implementation spans the template, `NAVIGATION`/`GROUPS` in `guide_content.py`, the guide route,
guide CSS and the guide block in `app.js`. Content describes current behavior, including
read-only TikTok and unavailable social downloads. Session-revocation instructions explicitly
say that the current session remains signed in, in both languages.

## Access and action boundaries

The guide is an authenticated, consent-gated, no-store GET. Reading it cannot start imports,
reserve slots, publish, upload drafts, create work or mutate the database. The global publishing
lock remains in place, and plans remain paused during testing.

Person-specific links retain the authenticated, server-revalidated selection when a person is
selected. Guide query parameters cannot select a person. Global connection management links
go to /accounts, separately from the selected-person account wizard link.

## Final review

**Independent finish verdict: SHIP. No open material findings.** This disposition covers the
revised layout and does not imply user acceptance, final CI, deployment or live platform
acceptance.

| Finding | Resolution | Evidence |
| --- | --- | --- |
| P2 — Oversized full-article focus | RESOLVED: fragment target and focusability moved to the H2. | Source and English desktop capture show compact heading focus. |
| P3 — Lateral RTL disclosure chevrons | RESOLVED: physical right/bottom borders preserve orientation. | Arabic desktop capture shows down/up chevrons. |

## Verification and evidence

The mechanical layout scan returned `[]` before and after the revision. Ruff, formatting and
JavaScript syntax checks were clean. The guide/usability/compliance suite passed 111 tests;
after the final fixes, all 28 guide tests passed.

Browser checks verified topic selection, Next, language changes preserving the topic, legacy
`#drive` migration and no horizontal overflow at 390px, 960px or 1728px. The viewport was reset
after verification.

Current evidence directory: `/Users/mac/dev/repeter-preview/guide-layout-20261018`. Captures
are shown at their actual sizes; some depict scrolled fragment-navigation states rather than
the page top.

| Capture | Coverage |
| --- | --- |
| guide-final-ar-desktop.jpg | Arabic, light theme, 1728 × 879. |
| guide-final-ar-mobile.jpg | Arabic, light theme, 390 × 844. |
| guide-final-en-dark-desktop.jpg | English, dark theme, 1728 × 879. |
| guide-final-en-dark-mobile.jpg | English, dark theme, 390 × 844. |
| guide-final-en-dark-tablet.jpg | English, dark theme, 960 × 900. |

All captures and browser checks used synthetic local fixtures. No publication, draft upload
or real-account mutation was performed. Actual screen-reader interaction was not tested.
Final CI and deployment have not been claimed.
