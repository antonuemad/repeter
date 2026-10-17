---
version: 1
slug: "repeater-web-templates-guide-html"
primary_target: "repeater/web/templates/guide.html"
related_targets: ["repeater/web/guide_content.py","repeater/web/routes/guide.py"]
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

The desktop layout places a native numbered chapter rail beside a reading column bounded to
75ch. The sticky rail starts at 80px and has an adjusted maximum height to remain below the
utility bar while allowing its own scrolling. On mobile, the native chapter index is collapsed;
progressive enhancement closes it after chapter selection and moves keyboard focus to the
target. The complete document remains available without JavaScript. Numbered native task
lists, stable chapter anchors and back-to-contents links support sequential reading and direct
navigation in both themes.

Implementation spans repeater/web/templates/guide.html, repeater/web/guide_content.py,
repeater/web/routes/guide.py, supporting guide CSS and progressive index JavaScript. Content
describes current behavior, including read-only TikTok and unavailable social downloads.
Session-revocation instructions explicitly say that the current session remains signed in,
in both languages.

## Access and action boundaries

The guide is an authenticated, consent-gated, no-store GET. Reading it cannot start imports,
reserve slots, publish, upload drafts, create work or mutate the database. The global publishing
lock remains in place, and plans remain paused during testing.

Person-specific links retain the authenticated, server-revalidated selection when a person is
selected. Guide query parameters cannot select a person. Global connection management links
go to /accounts, separately from the selected-person account wizard link.

## Final review

**Disposition: SHIP. No open material findings.** This is the final surface-review disposition;
it does not assert final CI, deployment or live platform acceptance.

| Finding | Resolution | Evidence |
| --- | --- | --- |
| F1 — Global connection-management action | RESOLVED: /accounts is separate from the selected-person wizard link. | Rendered regression. |
| F2 — Sticky desktop chapter rail | RESOLVED: 80px top offset and adjusted maximum height. | Source and scrolled screenshot: railTop 80px; utilityBottom 57px. |
| F3 — Session-revocation wording | RESOLVED: both languages retain the current signed-in session. | Arabic and English content. |

## Verification and evidence

Before the material fixes, 165 guide, customer journey, authentication, theme and compliance
regressions passed. After the fixes, all 19 guide tests passed. Ruff and JavaScript syntax
checks were clean. Browser checks confirmed no horizontal overflow, mobile index closure on
chapter selection and keyboard focus at the target.

Evidence directory: /Users/mac/dev/repeter-preview/guide-20261018.

| Capture | Coverage |
| --- | --- |
| guide-final-ar-desktop.jpg | Arabic, light theme, desktop. |
| guide-final-ar-mobile.jpg | Arabic, light theme, 390px mobile. |
| guide-final-ar-scrolled-desktop.jpg | Arabic desktop after scrolling; rail clearance. |
| guide-final-en-dark-desktop.jpg | English, dark theme, desktop. |
| guide-final-en-dark-mobile.jpg | English, dark theme, recaptured at 390 × 844. |

All captures and browser checks used synthetic local fixtures. No real publication or draft
upload was performed. Source review does not establish live platform acceptance. Actual
screen-reader interaction was not tested. Final CI and deployment have not been claimed.
