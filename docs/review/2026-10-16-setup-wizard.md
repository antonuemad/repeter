# Guided setup acceptance — 2026-10-16

The first customer now enters through `/setup`, names a person, and follows one wizard:

1. Connect accounts and save the person's selected active pages.
2. Connect Drive and browse/paste a folder, explicitly start/pause imports, or add content manually.
3. Save local days/times and rotation/repeat gap in one transaction.
4. Inspect the same 30-day read-only simulation, including repetitions, exclusions and empty slots.

OAuth success, cancellation and configured-provider failures return to the relevant authorized
wizard step. Folder results, pagination and validation keep content context. Stale revisions,
invalid combined schedules and page selection errors cannot partially overwrite a plan. Existing
advanced setup remains available, and existing plan modes are retained. New wizard plans use repeat
mode with a 30-day minimum. Each save persists choices; there is no autosave of unsaved edits.

The invitation acceptance test now traverses the wizard in both Arabic and English, including
fake Meta/Google callbacks and mixed media. `tests/test_setup_wizard.py` covers navigation,
repeat-preview pagination, empty content, missing pages, failed Drive access, folder result escaping,
explicit imports, invalid/replayed submissions, CSRF, foreign tenants and unsafe OAuth returns.
All external provider HTTP is denied unless explicitly mocked. Tests verify zero publication jobs
or reservations from setup; only an explicit import action may enqueue a source poll.

Local validation: 317 related tests passed, Ruff check and format check passed. Chrome review at
1440×1000, 768×1024 and 390×844 covered Arabic/English and light/dark. A synthetic text post rotated
five times with a seven-day gap, leaving 25 explained empty slots. The review added a direct return
from item details and moved lengthy preview explanations behind a disclosure. No horizontal page
overflow was observed. Browser logs contained extension message-channel errors; no application
rendering failure remained. CI and deployment details are recorded with the release receipt.

No schema migration, production customer creation, real OAuth grant, real import, plan activation or
publication was performed for this change. `REPEATER_PUBLISHING_ENABLED=false` must remain in both
web and worker. Google/Meta test eligibility and real customer permissions are unchanged launch gates;
TikTok and the optional notification channel remain deferred.
