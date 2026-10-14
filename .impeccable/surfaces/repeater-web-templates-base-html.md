---
version: 1
slug: "repeater-web-templates-base-html"
primary_target: "repeater/web/templates/base.html"
related_targets: ["repeater/web/templates/overview.html","repeater/web/templates/login.html","repeater/web/templates/library.html","repeater/web/templates/schedule.html","repeater/web/templates/accounts.html","repeater/web/templates/sources.html","repeater/web/templates/rules.html","repeater/web/templates/activity.html","repeater/web/static/app.css","repeater/web/static/app.js"]
---

# Repeater application shell and operational routes

Mode: Operate.

Scope: shared authenticated shell, login, overview, library, schedule, accounts, sources, rules, and activity. Legal and error routes inherit the same chrome and typography.

Audience and task: selected customers invited by Magic Lap. Sign in, connect owned accounts, prepare content, review explicit publishing rules, and monitor results. The overview should expose the next useful action and the real state of the workspace.

Chosen direction: the user pinned https://sentinel-eye.magic-lap.com/ as the approved family. The primary artifact's direction contract explicitly overrides seed c15f641f. Keep Repeater's own product name and content within that family. No generated comp was selected; the supplied reference and measured implementation are the authority.

First-view composition: familiar utility chrome, a compact route title and useful actions, then workspace status and task content. The overview pairs operational counts with ordered setup guidance, attention states, upcoming work, and account health. The memorable detail is the same calm Magic Lap shell in both reading directions, with the next publishing task immediately actionable.

Proof and content: derive counts, setup progress, attention items, and history from the workspace. Local preview records are synthetic and are never evidence of customer activity or publishing performance. The login form states invitation-only access and offers a support destination.

Constraints: preserve Arabic/English parity, native no-JavaScript navigation and forms, accessible keyboard focus, explicit publishing decisions, existing permission boundaries, CSRF, consent, and uncertain-publication safeguards. Theme selection is an explicit server form. DESIGN.md owns reusable visual rules; this brief does not establish a new palette or component vocabulary.

Finish evidence: independent finish review disposition was ship with no material fixes. One optional narrow-screen case remains: an exceptionally long workspace name can use further truncation testing. Treat this as a case to inspect, not a design rule to preserve. The readiness report owns integration and launch gates.

Open decisions: invitation delivery and customer lifecycle are not implemented by this visual update. PRODUCT.md and the readiness report track those product decisions.
