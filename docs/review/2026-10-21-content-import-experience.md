# Content folder and import usability

The customer now chooses a Google Drive account, searches folders by name, checks a result and
explicitly starts importing. A pasted folder link remains available in a secondary disclosure.
The selected folder has a read-only Drive link. Search, pagination, options and choosing a result
share one native form, so changing image/backfill options applies to the chosen folder without JS.
Drive name queries escape apostrophes/backslashes using Google's documented query-literal syntax:
https://developers.google.com/workspace/drive/api/guides/search-files

Progress reports actual persisted states/counts per owned person/source, not aggregate library
counts. Completed listing counts are saved only with a successful ingestion checkpoint. An explicit retry of a failed folder check shows queued/checking status and retains the previous error as context. Failed
items link to their existing detail/retry flow. Access, expired credentials, rate limits, invalid
links and temporary connectivity errors have distinct recovery copy; form values remain present.
Empty scans and new-only mode have separate explanations. Language/theme changes after a folder search or error return to the person’s readable Content page instead of a POST-only endpoint. No percentage, deadline or success
claim is inferred while discovery is still queued/running.

Visible pages refresh a DB-only, authenticated/no-store fragment every 10 seconds, up to 20 minutes.
Refresh stops on HTTP/auth/network errors and resumes explicitly. It updates the library summary,
retains form drafts, avoids replacing focused error details and does not consume flash messages.
Native forms and page refresh remain usable without JavaScript. Existing source lock, revisions,
CSRF, account status checks and workspace/person ownership are preserved. All wording is AR/EN.

Validation uses synthetic accounts, mocked HTTP and disposable local DB/media. There are 46 new
acceptance cases covering search/paging/escaping/options, error recovery, twelve progress states
in both languages, tenant/CSRF isolation, empty/new-only modes and successful scan checkpoints.
The affected 352-case regression run and final 147-case setup/guide acceptance run passed; these
runs overlap. The final language/theme and setup regression run passed 97 cases; the completed retry/monitoring recovery run passed 100 cases. Ruff and formatting pass. Full Linux CI on the pushed commit must pass before release.

The local importer run passed 164 cases and failed one FFmpeg-dependent voice-over deduplication
assertion. The same single case fails separately on the unchanged baseline; it does not call
the source-poll code changed here. No media/fingerprint behavior or test was weakened for this UX work.

Deployment retains both production publishing/TikTok-audit locks as false. Production checks are
read-only; no real library import, publication, draft upload or account mutation is used for QA.
Release artifacts under repeter-deploy/content-import-20261021 record exact CI SHA, source hashes,
web/worker runtime verification, before/after publication counts and unchanged protected env.
