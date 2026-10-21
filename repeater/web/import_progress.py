"""Read-only source progress, scoped to one person's owned sources. No provider calls."""

from sqlalchemy import func, select

from repeater.jobs import kinds
from repeater.models import AccountStatus, ConnectedAccount, Job, JobStatus, Video, VideoStatus
from repeater.timeutil import ensure_aware, utcnow


def source_progress(db, plan, sources):
    sources = [s for s in sources if s is not None]
    ids = [s.id for s in sources]
    if not ids:
        return []
    counts = {}
    for sid, status, n in db.execute(
        select(Video.source_id, Video.status, func.count())
        .where(
            Video.workspace_id == plan.workspace_id,
            Video.profile_id == plan.profile_id,
            Video.source_id.in_(ids),
        )
        .group_by(Video.source_id, Video.status)
    ):
        counts.setdefault(sid, {})[status.value] = n
    polls = {
        job.dedupe_key: job
        for job in db.scalars(
            select(Job).where(
                Job.kind == kinds.POLL_SOURCE,
                Job.dedupe_key.in_([f"poll_source:{sid}" for sid in ids]),
                Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
        )
    }
    accounts = {
        a.id: a
        for a in db.scalars(
            select(ConnectedAccount).where(
                ConnectedAccount.workspace_id == plan.workspace_id,
                ConnectedAccount.id.in_([s.connected_account_id for s in sources]),
            )
        )
    }
    # Bound the per-source examples without loading a large failed library into memory.
    ranked = (
        select(
            Video.id,
            func.row_number().over(partition_by=Video.source_id, order_by=Video.id.desc()).label("n"),
        )
        .where(
            Video.workspace_id == plan.workspace_id,
            Video.profile_id == plan.profile_id,
            Video.source_id.in_(ids),
            Video.status == VideoStatus.FAILED,
        )
        .subquery()
    )
    failures = list(db.scalars(select(Video).join(ranked, Video.id == ranked.c.id).where(ranked.c.n <= 3)))
    rows = []
    for source in sources:
        c = counts.get(source.id, {})
        account = accounts.get(source.connected_account_id)
        poll = polls.get(f"poll_source:{source.id}")
        state = "watching" if c.get("ready") else "empty"
        if not source.last_success_at:
            state = "queued" if source.enabled else "saved"
        if c.get("failed"):
            state = "failed"
        if c.get("processing"):
            state = "processing"
        if c.get("importing"):
            state = "importing"
        if source.enabled and poll:
            state = "checking" if poll.status == JobStatus.RUNNING else "queued"
            if poll.status == JobStatus.QUEUED and ensure_aware(poll.run_at) > utcnow():
                state = "scheduled"
        if not source.enabled:
            state = (
                "paused"
                if source.last_polled_at or source.last_success_at or source.config.get("baseline_at")
                else "saved"
            )
        if source.last_error:
            state = "error"
        if account is None or account.status != AccountStatus.ACTIVE:
            state = "reconnect"
        rows.append(
            {
                "source": source,
                "counts": c,
                "total": sum(c.values()),
                "state": state,
                "pending": c.get("importing", 0) + c.get("processing", 0),
                "failures": [v for v in failures if v.source_id == source.id],
                "empty_scan": source.config.get("last_listed_count") == 0,
                "new_only": not source.config.get("backfill_count", 0),
            }
        )
    return rows
