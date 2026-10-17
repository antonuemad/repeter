# Operations acceptance — 2026-10-17

Publication remains prohibited, including drafts, containers, private/unlisted and test posts.
Both runtime locks remain false. Operational emails to the authorized operator are in scope.

Fixed automatic recovery selecting a closed workspace before an available open membership.
Regression coverage uses two memberships and asserts one delivered reset scoped to the open one.

Added a real-mail acceptance command that creates and removes a disposable database. It exercises
the forgot/open/confirm/login HTTP flow and actual encrypted outbox/SMTP, reads only the exact
expected message through read-only IMAP, verifies the link digest, old-session invalidation,
old-password rejection and one-time link use. No production password or consent is modified.

Monitor notification drills use separate state, suppress duplicates, and send exactly two TEST
emails for simulated failure/recovery. SMTP acknowledgement and recipient inbox verification are
separate evidence. GitHub can run this drill manually from outside the VPS; independent SMTP
secrets require the owner's authorization. HTTPS success alone does not prove alert delivery.

Media backup success now requires remote read-back comparison. A failed comparison cannot update
the overall/media-success receipts. The isolated recovery drill can require actual media and
compare a synthetic canary manifest (metadata, original/encoded/thumbnail SHA-256, text and paused
plan), then decrypt its synthetic probe in an offline Python process. No application or worker
starts against the restored database. Encryption keys travel only on standard input to that
offline process; independent secure custody of production keys remains an operator requirement.

An actual nonempty offsite restore exposed the image digest mismatch: images use `sha256` for
canonical-pixel deduplication, while the old drill assumed it always hashed original file bytes.
Migration0009 adds a separate `original_sha256`, preserves visual deduplication, and backfills
only legacy video digests. The drill supports older dumps without mistaking image pixel digests
for file digests; remote read-back and canary original/derivative manifests still verify bytes.
New image originals have their own raw digest, with tamper and migration regression coverage.

The corrected offsite drill restored three workspaces/eight library items and verified seventeen
referenced media files. Its synthetic canary verified three video/image/text records, six file
SHA-256 digests, disabled/paused state, and offline decryption. No live DB replacement, application
or worker startup occurred. Host and GitHub-origin TEST failure/recovery messages were each
found exactly once in the operator Inbox through exact-message read-only checks.

Capacity coverage adds eight synthetic tenants, sixteen real video/image uploads (~197MB), four
parallel upload sessions, bounded actual ffmpeg processing, and rotation previews. All sixteen
items become ready with tenant isolation, no provider HTTP, no publication jobs and no scheduled
posts. Existing coverage also simulates sixty plans across twenty workspaces and 1,200 text items.
These are bounded pipeline/preview checks, not live publishing throughput or an SLA.

Operational receipts and detailed logs are kept outside Git under
`/Users/mac/dev/repeter-deploy/operations-acceptance-20261017/`. Inspect their actual results before
claiming deployment, inbox delivery or offsite recovery success. Detailed recovery receipts on
the host are protected; the public status mount exposes only a success timestamp.
