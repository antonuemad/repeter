# Repeater

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The first trial is for selected customers invited by Magic Lap (confirmed by the
user, 2026-10-15). Creator and operator workflows already exist in PLAN.md and the
application. Customer account provisioning is currently manual; a self-service
invitation flow does not exist yet.

## Product Purpose

Keep a private short-video library and distribute the creator's own content to
connected accounts under explicit publishing rules. Success means an operator can
connect the correct account, import or upload content, review its settings and
schedule, and understand failures without causing duplicate posts.

## Operating Context

FastAPI/Jinja web application, PostgreSQL, an encoding and publishing worker, and
a single-host Docker deployment at https://repeater.magic-lap.com. Arabic and
English are supported, with workspace-local time. Source and publishing APIs have
different capabilities; not every platform is an official download source.

## Capabilities and Constraints

- Manual uploads, Google Drive, supported connected-platform sources and explicitly
  opted-in unofficial sources feed the library.
- Rotation rules, publication history, attention states, pause and reconnect are
  existing capabilities and must remain available.
- Real platform credentials are not configured on the deployed instance as of
  this review. Real publishing, platform approvals and customer onboarding remain
  launch gates; a healthy server does not establish publishing readiness.
- Preserve owner/editor authorization, tenant isolation, CSRF, explicit consent,
  platform-required settings and unknown-outcome publication safeguards.
- The installed database migration is immutable. Any later schema change needs
  a new Alembic revision.

## Brand Commitments

The user explicitly requires the approved Magic Lap UI/UX represented by
https://sentinel-eye.magic-lap.com/. Repeater keeps its own product name and content.
The old purple horizontal navigation is being replaced.

## Evidence on Hand

PLAN.md, HANDOFF.md, docs/review/2026-10-14-findings.md, tests/, current deployment
health checks, and the authenticated Sentinel Eye reference. Local preview data is
synthetic and must never be represented as live customer activity. No customer
results or publication performance claims have been supplied.

## Product Principles

- Show the next useful action and the real state of the workspace.
- Keep publishing decisions explicit and recover uncertain results carefully.
- Give Arabic and English equal navigation and form usability.
- Distinguish implemented functionality from verified live integrations.

## Open Decisions

Invitation delivery, customer lifecycle, commercial packaging, support commitments
and trial volume are not specified by this request. The review should identify
their impact without inventing pricing, customers or operating guarantees.
