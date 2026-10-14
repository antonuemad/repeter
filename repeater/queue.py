"""Postgres-backed job queue.

Jobs are rows in `jobs`. Workers claim with SELECT ... FOR UPDATE SKIP LOCKED, so several
worker threads/processes can run safely. `dedupe_key` prevents duplicate queued/running jobs
(e.g. one `poll_source:7` at a time) via a partial unique index.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from repeater.models import Job, JobStatus
from repeater.timeutil import utcnow


class RetryLater(Exception):  # noqa: N818 - control-flow signal, not an error
    """Raise from a handler to re-run the job later without counting it as a failure."""

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason or f"retry in {seconds}s")
        self.seconds = seconds
        self.reason = reason


def enqueue(
    session: Session,
    kind: str,
    payload: dict | None = None,
    *,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Insert a job. Returns its id, or None if an active job with the same dedupe_key exists."""
    values = {
        "kind": kind,
        "payload": payload or {},
        "status": JobStatus.QUEUED,
        "run_at": run_at or utcnow(),
        "dedupe_key": dedupe_key,
        "max_attempts": max_attempts,
        "attempts": 0,
    }
    stmt = pg_insert(Job).values(**values)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=and_(
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]), Job.dedupe_key.isnot(None)
            ),
        )
    stmt = stmt.returning(Job.id)
    return session.execute(stmt).scalar_one_or_none()


def claim_next(session: Session, worker_id: str, kinds: list[str] | None = None) -> Job | None:
    """Claim one due job and mark it running. Caller must commit."""
    now = utcnow()
    q = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED, Job.run_at <= now)
        .order_by(Job.run_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        q = q.where(Job.kind.in_(kinds))
    job = session.execute(q).scalar_one_or_none()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.locked_at = now
    job.locked_by = worker_id
    job.attempts += 1
    session.flush()
    return job


def complete(session: Session, job: Job) -> None:
    job.status = JobStatus.DONE
    job.finished_at = utcnow()
    job.locked_at = None
    job.locked_by = None


def reschedule(session: Session, job: Job, seconds: float, reason: str = "") -> None:
    """Put the job back in the queue without consuming an attempt."""
    job.status = JobStatus.QUEUED
    job.run_at = utcnow() + timedelta(seconds=seconds)
    job.attempts = max(0, job.attempts - 1)
    job.locked_at = None
    job.locked_by = None
    if reason:
        job.last_error = reason[:2000]


def fail(session: Session, job: Job, error: str, retry_in: timedelta | None = None) -> bool:
    """Record a failure. Returns True if the job will be retried."""
    job.last_error = error[:4000]
    job.locked_at = None
    job.locked_by = None
    if retry_in is not None and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.run_at = utcnow() + retry_in
        return True
    job.status = JobStatus.DEAD
    job.finished_at = utcnow()
    return False


def default_backoff(attempt: int) -> timedelta:
    """1 min, 5 min, 30 min, 2 h, 6 h ..."""
    steps = [60, 300, 1800, 7200, 21600]
    return timedelta(seconds=steps[min(max(attempt, 1), len(steps)) - 1])


def requeue_stale(session: Session, older_than_minutes: int) -> int:
    """Return crashed workers' jobs to the queue."""
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    res = session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, or_(Job.locked_at.is_(None), Job.locked_at < cutoff))
        .values(status=JobStatus.QUEUED, locked_at=None, locked_by=None, run_at=utcnow())
    )
    return res.rowcount or 0


def purge_finished(session: Session, older_than_days: int = 14) -> int:
    from sqlalchemy import delete

    cutoff = utcnow() - timedelta(days=older_than_days)
    res = session.execute(
        delete(Job).where(Job.status.in_([JobStatus.DONE, JobStatus.DEAD]), Job.finished_at < cutoff)
    )
    return res.rowcount or 0
