# Local customer acceptance — 22 October 2026

The first-customer preparation journey was exercised against a fresh, migrated local
PostgreSQL database and the actual web application in Chrome. Only an operator existed
initially. The customer workspace, invitation, password and person were created through
the application's HTTP routes. Browser testing started at the customer's sign-in screen.
Password setup/reset and synthetic consent were submitted by the HTTP acceptance harness,
not by entering a real person's new credentials or accepting real third-party terms.

## Safety and evidence boundary

- The entire journey used synthetic identities, tokens, folders and generated media.
- OAuth providers returned local, explicitly labelled mock authorization pages; no real
  Google, Meta, Instagram or TikTok consent was granted.
- Drive folder listing, metadata and downloads and TikTok identity/list responses were
  mocked. Unknown provider HTTP requests were denied, not passed through.
- Invitation/recovery used the real encrypted outbox and delivery handler with its SMTP
  transport replaced by a local capture. This does not establish real inbox delivery.
- Import jobs and ffmpeg/image processing ran for real against the disposable files.
- The local worker accepted only poll_source, import_video and process_video jobs.
  Both publishing and TikTok audit flags stayed false. No publication or draft upload
  was attempted against an external service.
- A direct HTTP request to start the customer's plan returned the testing-lock error.
  No scheduled posts or publish/poll jobs were created, and every plan remained disabled.
- This exercise neither changes the production deployment nor certifies external-user
  platform approval or live publishing acceptance.

## Exercised customer tasks

1. Create the customer through the operator screen's HTTP endpoint; capture their invitation.
   Reject a weak password, redeem the invitation, sign in, reject reuse of the invitation.
2. Start from the empty Home screen, add a person, and connect Facebook, Page-linked
   Instagram, direct Instagram, YouTube, TikTok and Google Drive through mock OAuth.
   Check return to the same person and explicitly select five destinations.
3. Search a Drive folder by name, preserve image/backfill options, choose it, verify that
   saving alone does not import, then explicitly start importing. A generated video and
   photo reached Ready through the real worker pipeline. A second scan added zero items.
4. Compose a text post and upload an image Story from the browser into the local library.
   Processing counts, readiness and recent-item status updated without a full reload.
5. Save a daily Cairo-time schedule and seven-day repeat interval; inspect the 30-day
   preview, compatibility exclusions and explained cooldown gaps. The preview remained
   a calculation with zero publication reservations.
6. Move one Facebook slot to 12:30 and replace its content; verify the changed slot in
   the unified calendar while the other destinations retain their own slots.
7. Bulk-exclude two items from Facebook, archive and restore the library with original
   files retained, then remove the exclusions. Review showed actual changes and no-ops.
8. Read the mocked TikTok empty-video view and its originals/reading-only explanation.
   Disconnect both Instagram accounts by platform, verify their disappearance from the
   account list and selectable destinations, reconnect using the original methods.
9. Request password recovery; capture one new reset message; redeem it; reject the old
   password and reused reset link; verify the new password and invalidation of both an
   HTTP session and the already-open Chrome session.
10. Deny another tenant's person/plan/library reads and archive mutation; deny customer
    access to platform administration and reject a mutation without CSRF.
11. Inspect Arabic desktop and 390px mobile and English dark mobile, language/theme
    transitions, the mobile navigation and guide. No horizontal document overflow was
    present in the checked views. This was viewport emulation, not a physical-device test.

## Corrections discovered in this exercise

- Import polling updated readiness numbers while leaving the old uncovered-pages warning,
  empty-library advice and recent-item states visible. Shared server-rendered guidance and
  recent-item fragments now refresh with the counts, preserve focused links, and keep
  no-JavaScript rendering. Arabic/English regression coverage checks empty → ready → archived
  transitions and verifies that the status GET creates no jobs or provider calls.
- The filter probe assumed every ffmpeg listing had three capability flags. The installed
  newer local ffmpeg emits two, causing even its legend to be parsed as a filter. The parser
  now supports both layouts and validates stream signatures; synthetic tests exercise both
  and reject legend entries.

The native macOS ffmpeg also lacks zscale and Chromaprint support. Its full application run
initially had nine media failures (2,265 passes, three skips); the parser issue was corrected,
and production-style Linux/ffmpeg verification runs separately inside local Docker. Missing
native codec features are not silently disabled or treated as successful HDR/audio tests.

## Final validation

- Full repository suite in a local Linux container: **2,499 passed, 13 skipped** in 300.57s.
  The thirteen skips were four ShellCheck cases, eight Compose-configuration cases and
  one root-only bootstrap case. They were then rerun in an isolated local container with
  those tools and container-root access: **13 passed**, zero skips. No Docker socket or
  production environment was mounted. Combined, all **2,512 collected cases passed**.
- Both test containers used a private Docker network without external connectivity and a
  dedicated disposable PostgreSQL container. Repository source was mounted read-only.
- New regression coverage: two language cases for live readiness/recent-state transitions,
  and two ffmpeg flag-layout cases. Existing media, invitation, OAuth, recovery, tenant,
  library, worker, deployment and safety tests ran in the full suite.
- Ruff lint, Ruff formatting, JavaScript syntax and Git whitespace checks passed.
- Final browser evidence includes Arabic desktop and mobile and English dark mobile.
  Actual import/download/processing, manual Story upload, library actions and password
  session invalidation were exercised in the local application as described above.

Local acceptance completed before releasing the fixes. Release verification must confirm
green CI for the exact deployed commit, matching source/assets in web and worker, the
unchanged protected environment and publishing locks, and read-only checks of the live
person/content/preview screens. Platform approvals, real customer account consent and any
later live-publication acceptance remain separate.

Detailed logs, captured test messages, fixture scripts, runtime counts and screenshots are
outside Git at `/Users/mac/dev/repeter-preview/customer-pilot-20261022/`.
