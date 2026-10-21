# Unified person home and preparation states

Scope: the first usability stage, preserving the approved Magic Lap identity and publishing lock.

- One owned person home with Pages, Content, Times and Preview sections. Default creation and
  navigation use it; OAuth, source control, folder forms and inline content return to it.
- Facebook/Instagram source forms are embedded in Content instead of sending customers to the
  advanced editor. Importing still needs an explicit action; opening/saving never publishes.
- Primary navigation is reduced. Accounts/activity/settings/legacy tools remain reachable in
  a secondary disclosure, with existing platform-administrator permissions preserved.
- Readiness requires usable owned content for the selected pages and a reviewed schedule.
  Migration 0010 retains historical explicit schedule saves without changing scheduling intent.
- Disabled selections remain hidden, expired choices expose a correct reconnect action, and
  selected disconnected/reassigned targets require review. Existing targets/history are retained.
- Preview warnings use person-plan connection/ownership state instead of legacy RotationRule
  completeness. Actual empty-slot reasons and unsafe/pending publication guards are unchanged.
- Format-incompatible platform controls explain the limitation and preserve saved preferences.
- Recovery copy follows SMTP configuration while keeping unknown/registered email responses
  identical. Home reconnects use direct Instagram or Meta as saved, and YouTube/Drive purposes.

## Evidence and boundaries

`tests/test_person_home.py` exercises the new journey in both languages, fake OAuth returns,
source controls, upload/text success and errors, stale revisions, CSRF, tenant isolation, invalid
return paths, compatibility/readiness, disconnected targets, provider-specific reconnection,
recovery wording and no-publication/no-draft behavior. Existing customer, library, rotation,
execution, account, guide and migration tests remain regression coverage. Migration coverage
checks metadata equivalence, upgrade/downgrade preservation and schedule-review backfill.

Rendered Chrome QA uses synthetic accounts in a separate local database, without a worker and
with outbound provider HTTP denied. Desktop Arabic/light and 390px Arabic/light + English/dark
checks confirmed navigation, native forms, disclosures, selected/expired choices and preview
pagination. No horizontal overflow was observed. Evidence lives outside the repository at
`/Users/mac/dev/repeter-preview/person-home-20261021/`.

No real account was connected/disconnected and no production media or plan was edited for QA.
The production deployment must retain publishing=false, TikTok audited=false and the protected
environment unchanged. CI and deployed source hashes are recorded against the delivered commit.
This is interface verification; it does not replace usability sessions with novice customers.
