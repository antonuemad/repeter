# TikTok read-only external review request — 17 October 2026

App `7697431346871896082` (Repeater), Individual ownership. Sandbox
`7697391841217841159` (Repeater pilot locked). The owner explicitly authorized submitting
**Login Kit with `user.info.basic` and `video.list` only**. Content Posting API,
`video.publish` and `video.upload` are deferred and excluded from the production request.

Submission and approval status must be read from the portal and the dated execution receipt.
A saved draft is not evidence of submission; submission is not approval. Production credentials
remain separate from the Sandbox credentials currently used by the running application.

## Scope and application behavior

| Permission | Implemented use | Evidence boundary |
| --- | --- | --- |
| `user.info.basic` | Show the authorized creator identity and verify its open_id. | The owner completes any login/consent; credentials and OTPs are excluded. |
| `video.list` | Display up to 20 latest public video metadata entries and safe viewing links. | The real Sandbox account currently returns zero public videos. No populated real list or original download is claimed. |

Every new TikTok authorization requests exactly these two scopes, independently of the global
publishing flag or audit flag. Code exchange reads the basic profile without creator_info even
if a legacy token response includes older publishing scopes. The callback opens the account's
read view, creates no publishing rule or planning job, and stops an existing legacy rule when
reconnecting. A validated setup-wizard return path is preserved.

The separate person's library contains clearly labelled synthetic originals uploaded only to
Repeater. Its rotation preview is a calculation, not a publication queue. All plans/rules stay
disabled, workspaces paused and the global publication gate closed. TikTok video.list supplies
metadata, not the original file; owned originals come from Drive or manual library upload.

## Submission fields

- Name: Repeater. Category: Productivity. Platform: Web.
- Website: https://repeater.magic-lap.com/about
- Privacy: https://repeater.magic-lap.com/privacy
- Terms: https://repeater.magic-lap.com/terms
- Redirect: https://repeater.magic-lap.com/oauth/tiktok/callback
- Products: Login Kit. Display API reading is selected through video.list in this portal.
- Requested scopes: user.info.basic, video.list. No Content Posting API or posting scopes.

**Review explanation:**

This submission requests Login Kit with user.info.basic and video.list ONLY. Each creator signs
into Repeater, chooses Connect TikTok and authorizes their own account through TikTok.
user.info.basic displays the connected creator identity; video.list reads public metadata and
viewing links. TikTok does not provide downloadable originals through this API. The current
Sandbox account returns zero public videos, shown honestly. Separate synthetic originals show
a personal library and rotation preview inside Repeater, which creates no posts. Content Posting
API, video.publish and video.upload are excluded and deferred. Global publishing is disabled;
no real post or draft upload is performed. The attached walkthrough is an edited sequence of
genuine Sandbox UI captures, with authentication completed by the owner separately.

## Reviewer walkthrough and video

1. Open the public About page and its Privacy/Terms links.
2. Open Accounts and choose Connect TikTok. Only identity and public video reading are requested.
   The owner performs any login, OTP or consent step. Never expose credentials in the recording.
3. Inspect the callback to the account-specific read view and refresh the list. Show the actual
   empty result as empty; connection errors have separate recovery guidance.
4. Inspect the separate synthetic library, selected destination, daily schedule, seven-day repeat
   interval and draft preview. Originals are stored in Repeater, not imported from TikTok.
5. Finish on the stopped plan and global publication lock. Do not enable any plan, resume the
   workspace, create a media container, publish or upload a draft, including private content.

Use the read-only revision of the walkthrough rather than the earlier four-scope preparation
video. Its filename, source captures, transcript, exact explanation, checksums and portal receipt
are retained outside Git in the dated `tiktok-readonly-20261017` deployment artifacts. Disclose
that the walkthrough is edited; do not present synthetic metadata as a genuine provider response.
A reviewer can request further continuous recording or a populated public account demonstration.

## Approval boundaries and later work

TikTok requires a developed external-facing product and evidence for all requested scopes.
The present request does not claim posting capability or approval. Existing Sandbox grants may
still list older permissions; this change does not silently revoke the owner's grant. New OAuth
requests are limited to the two read scopes and the lock prevents publication everywhere.

A later Content Posting API request requires a separate owner decision, creator-controlled
privacy/interactions/commercial disclosure, actual evidence accepted by TikTok and the necessary
approval. Do not enable `REPEATER_TIKTOK_AUDITED` when only Login Kit/video.list are approved.
Do not lift the owner's publication restriction to obtain review evidence.

## References checked on 17 October 2026

- [App review guidelines](https://developers.tiktok.com/doc/app-review-guidelines)
- [Display API setup](https://developers.tiktok.com/doc/display-api-get-started/)
- [Login Kit for Web](https://developers.tiktok.com/doc/login-kit-web)

## Operational verification

Both web and worker retain `REPEATER_PUBLISHING_ENABLED=false` and
`REPEATER_TIKTOK_AUDITED=false`. Tests use synthetic accounts and mocked HTTP. Runtime verification
checks exact source hashes, no enabled plans/rules, paused workspaces, zero scheduled/confirmed
publications and live identity/list reads through a strict read-endpoint allowlist.
