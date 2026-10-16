---
version: 1
slug: "repeater-web-templates-base-html"
primary_target: "repeater/web/templates/base.html"
related_targets: ["repeater/web/templates/overview.html","repeater/web/templates/login.html","repeater/web/templates/library.html","repeater/web/templates/schedule.html","repeater/web/templates/accounts.html","repeater/web/templates/sources.html","repeater/web/templates/rules.html","repeater/web/templates/activity.html","repeater/web/templates/settings_workspace.html","repeater/web/templates/settings_members.html","repeater/web/templates/settings_nav.html","repeater/web/templates/admin_customers.html","repeater/web/templates/admin_recovery.html","repeater/web/templates/access_open.html","repeater/web/templates/access_password.html","repeater/web/templates/access_forgot.html","repeater/web/templates/access_issued.html","repeater/web/static/app.css","repeater/web/static/app.js","repeater/web/templates/plans/index.html","repeater/web/templates/plans/start.html","repeater/web/templates/plans/wizard.html","repeater/web/templates/calendar.html","repeater/web/templates/calendar_edit.html","repeater/web/templates/billing.html","repeater/web/templates/admin_billing.html","repeater/web/templates/operations.html"]
---

# Repeater application shell and operational routes

Mode: Operate.

Scope: shared authenticated shell, login, overview, guided setup, people, accounts, library, weekly calendar and slot editing, activity, advanced sources/rules/legacy schedule, workspace settings/members/billing, operator customer administration/recovery/billing/service health, and public invitation/password setup/recovery forms. Legal and error routes inherit the same chrome and typography.

Audience and task: selected customers invited by Magic Lap. Sign in, create a person, connect and select their owned pages, prepare their library, and preview their rotation. The overview names the newest person and exposes the next useful preparation action. Legacy rules remain a secondary operational surface.

Access tasks: owners review members, issue or revoke editor invitations, and reauthenticate to change owner/editor/suspended access while preserving a last owner. Affected sessions are revoked. Platform operators create paused customer workspaces, invite owners, and verify identity before issuing a reset. Customers set their own passwords and accept legal terms. Invitation/reset messages use an encrypted outbox with a manual-link fallback; actual SMTP delivery remains unconfigured. Closure pauses work, disconnects accounts locally and revokes access; operator reopening leaves the workspace paused without restoring tokens.

Chosen direction: the user pinned https://sentinel-eye.magic-lap.com/ as the approved family. The primary artifact's direction contract explicitly overrides seed c15f641f. Keep Repeater's own product name and content within that family. No generated comp was selected; the supplied reference and measured implementation are the authority.

Navigation order: Overview → Get started → People → Accounts → Library → Calendar → Activity. Sources, Rules and the legacy Schedule live in an Advanced tools disclosure, followed by Settings. A separate operator-only disclosure contains Customers, Service health, and Plans & payment review. Native disclosures start open when a child is current. Settings has local links for workspace, members and billing.

First-view composition: familiar utility chrome, a compact route title and useful actions, then workspace status and task content. The overview pairs operational counts with ordered setup guidance, attention states, upcoming work, and account health. The memorable detail is the same calm Magic Lap shell in both reading directions, with the next publishing task immediately actionable.

Access composition: settings and members place compact local navigation beneath the title, followed by distinct task cards and flat member/invitation rows. Customer administration puts workspace creation before existing customers and pending invitations; recovery presents identity confirmation and issue/close actions within each request. Public access uses one centered task card with visible field labels, clear next action, and validation/status text. Copied links remain readable and selectable without clipboard support. Closure and billing verification use explicit reauthentication and explanatory copy beside their controls.

Calendar and commerce composition: calendar days label simulated previews separately from actual reservations, retain the person/page/content context and local timezone, and expose eligible slot edits. Time changes, exclusions, replacements and restoration stop the plan, bump its revision and cancel untouched work; they never activate publishing. Billing puts usage and invoice state before manual-transfer actions. Invoices preserve their requested amount and limits; a reference alone does not confirm funds. Operator approval requires a current password and an actual-funds verification checkbox. Unset prices/bank details render as unavailable, without invented offers.

Health composition: separate due-job queue age, local/offsite backup receipts, isolated restore-drill receipt, SMTP readiness and commercial setup. Receipt state/age are evidence with a stated scope. The external monitor links to GitHub for its actual latest result; local configuration does not claim successful external monitoring or delivered alerts.

Proof and content: derive counts, setup progress, attention items, and history from the workspace. Local preview records are synthetic and are never evidence of customer activity or publishing performance. The login form states invitation-only access and offers a support destination.

Constraints: preserve Arabic/English parity, native no-JavaScript navigation and forms, accessible keyboard focus, explicit publishing decisions, existing permission boundaries, CSRF, consent, and uncertain-publication safeguards. Theme selection is an explicit server form. DESIGN.md owns reusable visual rules; this brief does not establish a new palette or component vocabulary.

Historical finish evidence: the initial shell and the 2026-10-15 access extension each received an independent finish review with disposition ship and no material fixes. All five access-review sections passed against desktop and mobile captures. An exceptionally long workspace name remains an optional narrow-screen case from the initial shell review, not a design rule. The readiness report owns integration and launch gates.

Current delivery boundary (2026-10-16): encrypted customer invitation/reset mail and minimal operator alerts are implemented. Hostinger SSL/465 setup for info@magic-lap.com is ready, but its actual mailbox password is missing; no real email has been sent. Prices, bank details, support commitments and trial volume remain unsettled in PRODUCT.md. Existing registered email addresses cannot join a second workspace through this invitation flow. Existing platform scope and the publishing lock remain unchanged.

Operations finish evidence (2026-10-16): synthetic desktop/mobile captures are in /Users/mac/dev/repeter-preview/operations-ui. The first independent review disposition is fix: F1 shared native fields, F2 mobile activity, F3 service-health evidence, F4 current documentation. The final independent verdict is ship: F1 shared fields, F2 mobile activity, F3 health evidence and F4 documentation are all resolved in the final recaptures. No material findings remain. This UI verdict is not evidence of runtime deployment or live email delivery. Focused backend verification passed; broader Linux CI remains pending. The local broad run has an existing macOS ffmpeg voiceover failure and does not establish a clean full-suite result.

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
