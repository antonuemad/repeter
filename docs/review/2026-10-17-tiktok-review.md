# TikTok external review preparation — 17 October 2026

Status: **prepared, not submitted or approved**. App `7697431346871896082` (Repeater),
Individual ownership. Saved Sandbox `7697391841217841159` (Repeater pilot locked).
Production settings and the partial walkthrough attachment were saved as a draft and verified
after reopening the portal. No Submit for review action was taken.
Production and Sandbox credentials are separate and excluded from Git.

## Current evidence and its limits

| Requested permission / product | Evidence | Remaining gap |
| --- | --- | --- |
| Login Kit / `user.info.basic` | Owner completed Sandbox consent; live identity read succeeds. Accounts UI identifies the connected account. | A walkthrough assembled from genuine UI captures is an edited recording, not a continuous consent recording. Password/OTP steps are excluded. |
| `video.list` | Live public metadata list succeeds. Current account returns zero public videos; UI explicitly shows that result. | No populated real public list is claimed. Original files cannot be downloaded through this API. |
| `video.publish` / Direct Post | Implementation and mocked tests exist; global publication gate is closed. | No real post, private post, container creation, status polling or end-to-end publishing evidence is produced. |
| `video.upload` / upload-to-inbox | Implementation and mocked tests exist; global publication gate is closed. | No draft upload or completion evidence is produced. |
| Rotation | Separate, clearly labelled synthetic originals in a personal library; account selection, local schedule, repeat interval and preview. | Preview is not a posted or queued publication. Plan remains disabled and workspace paused. |

The walkthrough is partial evidence for connection, reading and scheduling preview only.
Do not submit it as proof that all four scopes have been demonstrated. Do not attest that
publication or draft upload succeeded. Do not imply that this Sandbox enables outside customers.

## Prepared walkthrough

`repeater-tiktok-read-and-rotation-walkthrough.mp4`: 119 seconds, H.264, 1920 × 1200,
1,528,411 bytes. It is an edited sequence of genuine UI captures with English captions;
the earlier owner consent capture is explicitly labelled. SHA256:
`44ffa5a55d987a1a1d3615f3c70636f9f3c33cbc73b1f6d998a011c5fbec5851`.
The portal draft retains this attachment and an explicit explanation of its evidence gaps.

## Submission copy (truthful draft)

**Name:** Repeater. **Category:** Productivity. **Platform:** Web.

**Description:** Organize your own videos, connect your accounts, and preview scheduled content rotation.

**Website:** https://repeater.magic-lap.com/about  
**Privacy:** https://repeater.magic-lap.com/privacy  
**Terms:** https://repeater.magic-lap.com/terms  
**Redirect:** https://repeater.magic-lap.com/oauth/tiktok/callback

**Review explanation:**

Repeater is a web service for independent creators to organize their own original media in a
personal library, connect their own accounts, select destinations, review platform settings and
preview a rotation schedule. Login Kit and user.info.basic identify the authorized creator.
video.list reads public video metadata and viewing links, not downloadable originals. Original
media is supplied by the creator through an authorized folder or manual upload. The attached
walkthrough shows a saved Sandbox connection, successful live reads and a preview using clearly
labelled synthetic originals. The connected account currently returns no public videos. Publishing
and draft uploads remain disabled during this invited pilot. video.publish and video.upload have
mocked tests only and are not demonstrated end to end. This package is a preparation draft and
must not be represented as a completed four-scope submission.

## Reviewer walkthrough

1. Open the public About page and inspect Terms and Privacy links.
2. Use a dedicated invited reviewer account supplied securely when the submission is ready.
   Do not embed working credentials or invite/reset tokens in Git, video or review notes.
3. In Accounts choose TikTok. The account owner completes login, scope choice and consent.
   Passwords, OTPs and legal confirmations are performed by the owner.
4. Return to Accounts; inspect connected identity and choose View TikTok videos.
5. Refresh the list. A successful empty response must remain empty; an API failure must show
   recovery guidance instead. Private videos and drafts are not included by video.list.
6. Open the demonstration person's setup: originals, destination, schedule, repeat interval,
   preview. Library clips are explicitly synthetic and were uploaded only to Repeater.
7. Confirm the global publication lock. Do not enable the plan, resume the workspace, publish,
   upload a draft, or try even a private test publication.

## What must be resolved before external submission

- TikTok requires a fully developed external-facing product; development/testing applications
  are not approved under the current review guidelines. Resolve this product-readiness gate.
- Every requested product and scope needs a genuine demonstration, using Sandbox for a first
  submission. The present no-publication instruction prevents evidence for publishing scopes.
  Only the owner can explicitly change that restriction. Do not lift it merely for review.
- An initial read-only Login Kit + video.list submission could avoid the publishing evidence
  gap if the portal/product configuration supports it. It would not authorize publishing.
  Do not silently remove products/scopes from the app or claim that route has been approved.
- Preserve creator choice for privacy, interactions, commercial disclosure and music consent.
  No unattended, unlimited, cross-platform download or approval promises are made.
- Final review credentials, refreshed continuous consent recording if requested, attachments
  and any portal attestations must be completed before clicking Submit.

## Official references checked on 17 October 2026

- [App review guidelines](https://developers.tiktok.com/doc/app-review-guidelines): complete
  product, Sandbox first demo, every product/scope visible, up to five videos, 50 MB per video.
- [Content sharing guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines):
  authentic original content, creator controls, broad audience, no internal-only uploader.
- [Content Posting API setup](https://developers.tiktok.com/doc/content-posting-api-get-started):
  application approval and user consent are separate requirements.

## Operational verification

Keep `REPEATER_PUBLISHING_ENABLED=false` and `REPEATER_TIKTOK_AUDITED=false` in web and worker.
No enabled rotation rule, enabled person plan, resumed workspace or confirmed publication is
permitted for this demonstration. Read-only checks allow identity/video-list endpoints only;
publication tests use mocked HTTP and synthetic accounts. Final video, source captures, subtitle
track, checksums and runtime receipts are stored outside Git under the dated deployment artifacts.
