# Library management — 2026-10-21

The incumbent Magic Lap/Sentinel Eye system remains: semantic colors, logical-start navigation,
self-hosted Inter/IBM Plex Sans Arabic, native forms and explicit testing lock. The task is selecting
and managing content for one person with as few page visits as possible.

Library adds literal title/caption search, visible platform-exclusion notes and an Excluded filter.
Search respects the selected person and saved title/caption overrides. Filter/paging links carry
search and person context; an emptied final page clamps to remaining results. Empty filters do not
suggest importing into a different library. Compact media thumbnails use containment rather than
cropping; selection labels have native checkboxes with item names.

One form owns page-local checkboxes through native form association, avoiding nested forms inside
existing card actions. The default page permits at most 24 explicit IDs. JS only enhances counts,
select-page/clear and action disclosure. The selected-action bar remains in reach while scrolling;
mobile uses two fields beside each other to retain room for content. No persistent cross-page or
implicit select-all-library operation exists. In-progress and duplicate rows explain why bulk
management is unavailable; existing individual recovery remains.

Available operations: exclude one/all supported platforms, remove those exclusions, archive, and
restore archived content with ready files/text. Files and other overrides are retained. A native
review page names items, scope and actual changes; Edit selection returns to the same bounded
selection. Preferences return to a readable GET, including after invalid/stale POSTs. Confirmation
expires after 10 minutes, binds the actor/workspace/session, and rechecks row digests/plan revisions
under workspace and row locks. Parallel confirmation produces one mutation, with stale/replayed
attempts rejected. No-op reviews state that nothing will pause.

Actual changes stop the owned person's plan and increment its revision. Untouched scheduled/failed
reservations are canceled, while published/in-flight/ambiguous attempts stay in history. Legacy
unassigned exclusion cancels only the chosen platform's untouched reservations. No jobs are enqueued;
no provider calls, file deletion, live content changes, draft uploads or scope changes occur.

Acceptance adds 66 cases with synthetic people/media and denied HTTP. Final application regression: 614 passed; final library/guide verification: 117 passed (overlapping suites). Cases cover both locales, all platform
scopes, reversible exclusion/archive, file preservation, unavailable batches, stale/deleted/reassigned
rows, expired/tampered/replayed tokens, concurrent confirmation, tenant/person/actor/CSRF boundaries,
native selection/editing, literal wildcard search, filtering/pagination, preference returns and
preserved publication history. Full affected application regression plus exact-head Linux/Docker
CI are required before deployment. Local macOS Bash 3 cannot run unmodified Linux deployment-shell
tests; CI includes those tests and disposable backup/restore acceptance.

Visual acceptance covers Arabic/light and English/dark at desktop and 390px mobile, using only a
local synthetic fixture with no worker. Production verification is read-only. Release verification
matches exact CI SHA/runtime files, checks unchanged protected environment and compares before/after
lock, plan, post/job and account status audits. No new design ignores or identity changes are needed.
