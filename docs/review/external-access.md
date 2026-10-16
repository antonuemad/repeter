# External customer access — Google and Meta

Prepared 2026-10-16. This is a submission preparation pack, **not an approval receipt**.
Publishing remains `REPEATER_PUBLISHING_ENABLED=false` in both web and worker. All
acceptance tests use synthetic accounts and mocked HTTP. No real posts, private uploads,
Stories, drafts, scheduled publications or publishing API probes are permitted.

## Application details

- Product/operator: Repeater / Magic Lap.
- Public product page: https://repeater.magic-lap.com/about
- Privacy: https://repeater.magic-lap.com/privacy
- Terms: https://repeater.magic-lap.com/terms
- Support/deletion contact: info@magic-lap.com
- Google project: `repeater-pilot`; current audience External / Testing.
- Meta parent app: Repeater, `1263059295992502`; currently unpublished.
- Instagram app: Repeater-IG, `3279503472439957` (distinct credentials).
- No external customer is selected yet. Do not invent customer identities or reviewer credentials.
- App secrets must stay in the protected production environment, never in this pack, recordings,
  source control, tickets or screenshots.

## Google submission draft

**Purpose (copy-ready):** Repeater lets invited creators organize their own media into a
person's library, connect their accounts, choose content sources and destinations, and
preview a daily rotation with a minimum interval before repeating an item. Google
connections are authorized by the user in Google's browser consent flow. Account tokens
are encrypted at rest. Users can disconnect accounts and request data deletion.

| Scope | Implemented use / justification |
| --- | --- |
| `openid`, `https://www.googleapis.com/auth/userinfo.email` | Identify the Google grant and show the selected Drive account; prevent revoking a grant still used by another workspace. These do not replace Repeater invitation authentication. |
| `https://www.googleapis.com/auth/drive.readonly` | List folders and download supported video/image files in folders chosen by the user, including files added later. The permission technically covers the whole Drive account; this is disclosed before consent and in privacy copy. Repeater does not edit Drive files. |
| `https://www.googleapis.com/auth/youtube.readonly` | Identify the channel and uploads playlist, detect new uploads and import listing metadata. Original YouTube video files must come from the customer's Drive/library; the official API provides no download. |
| `https://www.googleapis.com/auth/youtube.upload` | Upload the user's reviewed original media to the channel with explicit audience, visibility and metadata settings after production publishing is authorized. Currently blocked by the global test lock. Do not claim a successful live upload demonstration. |

Redirect URI: `https://repeater.magic-lap.com/oauth/google/callback`.
Authorized domain: `magic-lap.com`. Home page must be `/about`, not a sign-in-only page.
Keep developer/support contacts accurate and verify domain ownership in Search Console.
Do not use a customer account as the developer of a separate OAuth application.

### Google gates before general availability

1. Confirm branding, contact information, homepage, policies and domain ownership; prepare
   brand verification. Use a production Google project/client policy appropriate to release;
   do not simply flip Testing to Production to avoid verification.
2. Submit exact sensitive/restricted scope justifications and a genuine consent/feature
   demonstration. Record login/consent, account choice, folder selection, import and preview
   with owner-approved test assets. Hide tokens, email not intended for reviewers, unrelated files.
   **This session authorizes simulation only, so that real-account recording remains pending.**
3. `drive.readonly` is restricted. Server-side access/storage requires restricted-scope
   verification and normally an annual independent security assessment unless Google grants
   an applicable exception. No assessor or paid commitment is authorized by this pack.
4. Consider a separately scoped `drive.file` + Google Picker product flow if the assessment
   is unsuitable. Do not substitute the scope alone: existing-folder access, child files and
   future-file discovery need a designed and tested alternative. Current code keeps readonly.
5. Resolve any separate YouTube API compliance/audit requirements before enabling publishing.
   OAuth approval is not proof that video publishing is approved. No real upload is allowed
   to demonstrate this while the user-imposed lock is active.
6. Keep the limited pilot allowlist until these gates are resolved. Google Workspace admins
   may still block third-party apps; approval does not override the customer's organization.

## Meta submission draft

**Purpose (copy-ready):** Repeater connects professional accounts belonging to the creator
or business using the service. The user chooses destinations and owned media sources,
organizes that media in a personal library and previews the planned rotation. Direct
Instagram Login does not require a Facebook Page. Facebook Login remains available for
Pages and Page-linked Instagram accounts. Repeater does not request Instagram messaging
or comment-management permissions. The current invited pilot disables all publishing.

| Permission | Concrete review evidence / feature |
| --- | --- |
| `instagram_business_basic` | Direct Instagram login, professional profile identification, listing and importing the account's own supported video media. |
| `instagram_business_content_publish` | Reviewed image posts, Reels and eligible Business-account Stories through the direct Instagram adapter. Demonstrated by mock tests only while locked. |
| `pages_show_list` | Display the Facebook Pages the user authorized and choose destination/source Pages. |
| `pages_read_engagement` | Read owned Page data/media needed by the selected source. |
| `pages_manage_posts` | Prepared Page publishing flow; global lock currently blocks execution. |
| `instagram_basic`, `instagram_content_publish` | Existing Facebook-linked Instagram account discovery, owned media and prepared publishing flow. These are separate from the direct-login scope family. |
| `business_management`, `ads_read` | Existing Facebook Login requests these for Page/business-role access. Re-evaluate necessity against the exact reviewer flow and current Meta requirements; no Ads dashboard or advertising feature exists. Do not claim one or add these to direct Instagram Login. |

Do not click “Add all required messaging permissions”: this product needs neither
`instagram_business_manage_comments` nor `instagram_business_manage_messages`.

### Direct Instagram settings

| Setting | Value |
| --- | --- |
| Valid OAuth redirect URI | `https://repeater.magic-lap.com/oauth/instagram/callback` |
| Deauthorize callback | `https://repeater.magic-lap.com/instagram/deauthorize` |
| Data deletion request callback | `https://repeater.magic-lap.com/instagram/data-deletion` |
| Privacy policy | `https://repeater.magic-lap.com/privacy` |
| Terms | `https://repeater.magic-lap.com/terms` |
| Server environment | `REPEATER_INSTAGRAM_APP_ID`, `REPEATER_INSTAGRAM_APP_SECRET` |

Callbacks use the Instagram secret and app-scoped Instagram identity, independently of
Facebook callbacks. A deletion request returns a confirmation code and public status URL.
Direct tokens refresh before expiry; an expired/revoked token requires reconnecting.
Disconnect removes only this workspace's token. Removing the app in Instagram's Apps and
websites settings revokes the full grant and triggers the deauthorization callback.

### Meta gates before external businesses

- Complete the Tech Provider/access verification and business verification requested by Meta;
  only the owner supplies legal business identity/documents. An app role is not proof of
  permission to access an unrelated customer's business assets.
- Request Advanced Access/App Review for implemented permissions. Ready for testing/Standard
  Access is **not** approval for arbitrary customers. Keep the app unpublished until requirements
  and reviewer evidence are complete.
- Reviewer access must be an expiring invitation to an isolated workspace; supply its private
  link through Meta's review form, never in this repository. No such reviewer invitation has
  been created yet. Reviewers should see no existing customers' accounts or libraries.
- Use a truthful recording of supported consent and preparation steps when separately
  authorized. A mocked success is not evidence of a real provider connection or publication.
  If review requires actual publishing, pause that evidence step until the owner changes the
  no-publishing instruction. Do not label mock results as provider approval.
- Instagram personal accounts must be converted by their owner to Business/Creator. Stories
  remain subject to account/platform eligibility. Existing Facebook-linked accounts reconnect
  through their original login method; automatic migration is intentionally refused to keep
  their library and platform state intact.

## Reviewer walkthrough and simulation evidence

1. Open `/about`, policies and login without authentication; product, operator and contact visible.
2. Accept an isolated invitation, set password and accept the current legal version.
3. Create a person at `/setup`; select Instagram directly or Facebook, with clear eligibility copy.
4. In a simulated flow, verify state ownership, exact callback, declined permissions, replay and
   cross-provider rejection, encrypted tokens and paused defaults. No scheduled posts created.
5. Select the person's accounts; choose a synthetic Drive folder or a fake platform video source.
6. Prepare a mixed library; choose schedule and repeat interval; preview repeatedly. The preview
   neither consumes content nor queues publishing. Tenant isolation remains enforced.
7. Disconnect/reconnect using the original provider. Signed deauthorization wipes matching tokens;
   signed deletion removes matching imports even after account retention without cross-provider deletion.
8. Confirm web/worker publishing false, zero created posts, all tests/CI green and current release hashes.

Tests: `tests/test_instagram_login.py`, `tests/test_customer_journey.py`,
`tests/test_web_oauth.py`, `tests/test_setup_wizard.py`, `tests/test_worker.py`.
Mock-only publisher tests cover direct-host Reels, image posts, image/video Stories and the
lock blocking publish/poll before any request. No provider tokens or accounts are used.

## Official references (checked 2026-10-16)

- [Google restricted scopes](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
- [Google OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies)
- [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [YouTube compliance audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
- [Instagram Login](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login)
- [Instagram profile and media](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/get-started)
- [Instagram migration guidance](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/migration-guide)

This pack and synthetic tests do not certify policy compliance, external-user approval or
live API acceptance. Record actual provider decisions and console status separately.
