# Invited-customer journey review — 2026-10-16

Scope: invitation/password setup → sign-in → person → connected accounts and selected pages →
optional Drive folder → mixed library → schedule and repeat interval → preview. No real platform
publication, platform import or production customer record was created for this review.

| Finding | Correction | Regression evidence |
| --- | --- | --- |
| Overview led new customers into legacy rules and asked them to enable publishing | Person-led steps and direct preview CTA; no publishing gate is changed | First-run, unassigned-content and person-specific readiness tests |
| A connected account or another library could falsely complete onboarding | Count only selected active pages and ready content for the named person | Processing files, other person, unselected and disconnected accounts |
| Library navigation lost the plan context | Direct setup/preview links and plan-specific empty-state recovery | Full customer flow and person library assertions |
| Invalid uploads/text creation discarded work and returned to the unassigned library | Render the same authorized person's form with bounded escaped drafts; require file reselection | Invalid raster, byte limits, long Arabic draft, retry and foreign-person rejection |
| Item edit errors discarded text and platform/placement choices | Render the submitted values without mutating the stored item | Failed edit followed by a successful retry |
| Past start dates blocked editing an ongoing schedule | Allow the unchanged saved start; still reject a newly supplied past date | Schedule edit and future-only preview regression |
| New invitation pages ignored the customer's workspace language | Apply workspace language after validating the private invite, unless explicitly chosen | English default and preserved Arabic preference |
| Folder setup appeared mandatory | Explain the direct upload/text alternative in setup | Rendered setup review in both languages |

`tests/test_customer_journey.py` traverses the complete journey in Arabic and English using a newly
invited customer in a separate workspace. OAuth providers are fake, Drive HTTP is mocked, images are
generated in memory, and the video fixture is synthetic. It verifies mixed-content rotation and
repeats, cross-tenant denial, stable repeated previews, rejected activation, paused imports/rules,
zero scheduled posts and zero publish/poll jobs. An internal manual-upload source is not a monitored
platform source. Unmocked external HTTP fails the test.

Local validation: 419 related tests passed across access, OAuth, library, plans, rotation, source
monitoring, rich publishing, lock and web regressions. After final navigation refinements, all 99
customer-journey/compliance/page tests passed again. Ruff check, format check and diff whitespace
checks passed. Exact-commit CI and deployment evidence are recorded in the release receipt.

Chrome review used local synthetic data: create person, select a fake page, save seven-day rotation,
recover a whitespace-title error without losing Arabic text, save the text post, return through the
library and inspect its five simulated uses over 30 days. Desktop 1440×1000, intermediate 768×1024 and
mobile 390×844 were checked, with Arabic/English and light/dark views. No page-level horizontal
overflow was observed. Browser logs contained extension message-channel noise; a local stale-code
render during development was resolved by restarting the preview server and rechecking the item.

Remaining launch gates: real platform approval and account permission constraints have not been
accepted by publishing during testing. TikTok remains deferred. Invitations/recovery remain manually
delivered. The server lock must remain `REPEATER_PUBLISHING_ENABLED=false`; this release does not
authorize enabling plans, rules, sources, workspaces or real publishing.
