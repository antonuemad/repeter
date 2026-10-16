---
version: 1
slug: "repeater-web-templates-base-html"
primary_target: "repeater/web/templates/base.html"
related_targets: ["repeater/web/templates/overview.html","repeater/web/templates/login.html","repeater/web/templates/library.html","repeater/web/templates/schedule.html","repeater/web/templates/accounts.html","repeater/web/templates/sources.html","repeater/web/templates/rules.html","repeater/web/templates/activity.html","repeater/web/templates/settings_workspace.html","repeater/web/templates/settings_members.html","repeater/web/templates/settings_nav.html","repeater/web/templates/admin_customers.html","repeater/web/templates/admin_recovery.html","repeater/web/templates/access_open.html","repeater/web/templates/access_password.html","repeater/web/templates/access_forgot.html","repeater/web/templates/access_issued.html","repeater/web/static/app.css","repeater/web/static/app.js"]
---

# Repeater application shell and operational routes

Mode: Operate.

Scope: shared authenticated shell, login, overview, library, schedule, accounts, sources, rules, activity, standalone workspace settings and members subnavigation, customer administration and recovery, and public invitation/password setup/recovery forms. Legal and error routes inherit the same chrome and typography.

Audience and task: selected customers invited by Magic Lap. Sign in, create a person, connect and select their owned pages, prepare their library, and preview their rotation. The overview names the newest person and exposes the next useful preparation action. Legacy rules remain a secondary operational surface.

Access tasks: owners review members and issue or revoke editor invitations. Platform operators create paused customer workspaces, invite owners, and review recovery requests. Operators verify identity before issuing a reset. Customers open private links, set their own passwords, and explicitly accept legal terms when joining. Invitation and recovery links are copied and delivered manually; no automatic email is sent.

Chosen direction: the user pinned https://sentinel-eye.magic-lap.com/ as the approved family. The primary artifact's direction contract explicitly overrides seed c15f641f. Keep Repeater's own product name and content within that family. No generated comp was selected; the supplied reference and measured implementation are the authority.

First-view composition: familiar utility chrome, a compact route title and useful actions, then workspace status and task content. The overview pairs operational counts with ordered setup guidance, attention states, upcoming work, and account health. The memorable detail is the same calm Magic Lap shell in both reading directions, with the next publishing task immediately actionable.

Access composition: settings and members place compact local navigation beneath the title, followed by distinct task cards and flat member/invitation rows. Customer administration puts workspace creation before existing customers and pending invitations; recovery presents identity confirmation and issue/close actions within each request. Public access uses one centered task card with visible field labels, clear next action, and validation/status text. Copied links remain readable and selectable without clipboard support.

Proof and content: derive counts, setup progress, attention items, and history from the workspace. Local preview records are synthetic and are never evidence of customer activity or publishing performance. The login form states invitation-only access and offers a support destination.

Constraints: preserve Arabic/English parity, native no-JavaScript navigation and forms, accessible keyboard focus, explicit publishing decisions, existing permission boundaries, CSRF, consent, and uncertain-publication safeguards. Theme selection is an explicit server form. DESIGN.md owns reusable visual rules; this brief does not establish a new palette or component vocabulary.

Finish evidence: the initial shell and the 2026-10-15 access extension each received an independent finish review with disposition ship and no material fixes. All five access-review sections passed against desktop and mobile captures. An exceptionally long workspace name remains an optional narrow-screen case from the initial shell review, not a design rule. The readiness report owns integration and launch gates.

Open decisions: manual invitation and recovery delivery is settled. Full member removal, workspace closure, packaging, support commitments, and trial volume remain product decisions tracked by PRODUCT.md and the readiness report. Existing registered email addresses cannot join a second workspace through this initial invitation flow.

2026-10-16 hardening: person-scoped preparation replaces the legacy rule-activation checklist. Three
completed preparation steps lead to the preview, never imply publication readiness. Libraries retain
drafts on validation errors and link back to the same plan. The incumbent tokens/components remain.
The local synthetic journey was walked in Chrome, including error recovery, content creation, a
seven-day repeat preview, Arabic/English and light/dark at desktop/mobile sizes. No horizontal page
overflow was observed at 390, 768 and 1440 pixels. This is implementation QA, not customer research.

2026-10-16 guided setup: `/setup` and the per-person `/setup/{step}` routes now keep the core journey
in four ordered steps. Reuse Magic Lap controls, typography, cards and semantic colors. The step
navigation uses real links and aria-current; current step is not a claim of completed setup. Save
buttons advance accounts and scheduling; folder browsing/import status remain within content.
Advanced options use native disclosures and the existing editor stays reachable. Final preview
prioritizes coverage and actual simulated rows, with detailed calculation/format help disclosed.
Native forms work without JavaScript; the page filter remains a progressive enhancement.
Local Chrome QA covered a synthetic person, selected page, manual text and seven-day repeat preview,
Arabic/English, light/dark, 390/768/1440 widths and no document overflow. OAuth/folder branches and
errors were exercised by mocked HTTP acceptance tests. No real content was imported or published.
