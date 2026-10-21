# Preparation feedback corrections — 21 October 2026

The page selector in both guided setup and the advanced editor now excludes explicitly
disconnected accounts. Connections needing renewed authorization remain visible and disabled
until reconnected. Account records, saved targets and imported library content are retained;
the existing server-side target validation still rejects disconnected IDs.

TikTok connection descriptions in Accounts and setup now state profile/public-video reading
only, without publishing or draft uploads. OAuth scopes and provider access are unchanged.

Empty preview headings use the actual common reason across the slot's pages. Mixed reasons
use a neutral heading that directs the user to each page. Disconnection, authorization renewal
and changed person assignment are distinct; previews and Calendar link back to page setup.
Repeat intervals, pending outcomes and explicit exclusions retain their own messages. Having
no ready content no longer implies the entire library is empty.

Regression coverage uses synthetic accounts, fake OAuth/revocation adapters and denied
unmocked HTTP. It checks single/platform/all disconnection, hidden target rejection, reconnect,
tenant/person boundaries, both locales, seven preview/calendar blockers and mixed causes.
The tests verify that reading or recalculating never creates publishing work. Production
must retain `REPEATER_PUBLISHING_ENABLED=false` and `REPEATER_TIKTOK_AUDITED=false`.

Local verification passed 406 focused tests, including 24 new regression cases, plus Ruff
lint/format checks. Synthetic Chrome inspection covered the English desktop selector and
Arabic mobile preview at 390 px without document overflow. No worker or real credentials
were used. Captures are retained outside Git in
`/Users/mac/dev/repeter-preview/preparation-feedback-20261021/`.

The existing Magic Lap design system and typeface remain intentional; no reusable visual
tokens changed. CI, runtime hashes and deployed UI must be checked separately from local
test or visual evidence. No live platform publication, media container or draft upload is
part of acceptance.
