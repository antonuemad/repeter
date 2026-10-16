"""Postgres-backed job queue.

Jobs are rows in `jobs`. Workers claim with SELECT ... FOR UPDATE SKIP LOCKED, so several
worker threads/processes can run safely. `dedupe_key` prevents duplicate queued/running jobs
(e.g. one `poll_source:7` at a time) via a partial unique index.

Claim order: priority class of the job kind first (publishing and polling before planning before
polling sources before bulk imports and encodes), then run_at, then id. A backlog of imports
therefore never delays a due publish_post or poll_post.

Leases: a running job's `locked_at` is its lease. The worker renews it every
settings.job_lease_refresh_seconds (renew_lease), so requeue_stale only returns the jobs of
workers that died (their lease lapsed), however long a live job runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from repeater.config import get_settings
from repeater.models import Job, JobStatus
from repeater.timeutil import utcnow

# Must match the predicate of the uq_job_dedupe_active index in repeater.models.Job.
_DEDUPE_INDEX_WHERE = text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL")


# Claim priority per job kind (lower is claimed first); see the module docstring. Kinds are
# spelled out here because repeater.jobs.kinds imports this module.
PRIORITY_PUBLISH = 0
PRIORITY_ENGINE = 1
PRIORITY_SOURCE = 2
PRIORITY_BULK = 3
KIND_PRIORITY: dict[str, int] = {
    "publish_post": PRIORITY_PUBLISH,
    "poll_post": PRIORITY_PUBLISH,
    "plan_workspace": PRIORITY_ENGINE,
    "refresh_tokens": PRIORITY_ENGINE,
    "maintenance": PRIORITY_ENGINE,
    "deliver_mail": PRIORITY_ENGINE,
    "delete_user_data": PRIORITY_ENGINE,
    "poll_source": PRIORITY_SOURCE,
    "import_video": PRIORITY_BULK,
    "process_video": PRIORITY_BULK,
}
DEFAULT_PRIORITY = PRIORITY_ENGINE


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
        # Literal SQL, not bound parameters: once psycopg prepares the statement (after a few
        # executions) Postgres can no longer match parameterised predicates to the partial index.
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=_DEDUPE_INDEX_WHERE)
    stmt = stmt.returning(Job.id)
    return session.execute(stmt).scalar_one_or_none()


def claim_next(session: Session, worker_id: str, kinds: list[str] | None = None) -> Job | None:
    """Claim the most urgent due job (priority class, then run_at) and mark it running.
    Caller must commit."""
    now = utcnow()
    priority = case(KIND_PRIORITY, value=Job.kind, else_=DEFAULT_PRIORITY)
    q = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED, Job.run_at <= now)
        .order_by(priority, Job.run_at, Job.id)
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


# The lease must outlive this many missed renewals before a job counts as abandoned.
LEASE_MISSES_TOLERATED = 3


def lease_timeout_minutes() -> float:
    """How long a running job may go without renewing its lease before it is requeued: the
    configured job_lock_timeout_minutes, but never less than a few renewal intervals."""
    settings = get_settings()
    floor = LEASE_MISSES_TOLERATED * max(settings.job_lease_refresh_seconds, 1) / 60
    return max(float(settings.job_lock_timeout_minutes), floor)


def renew_lease(session: Session, job_id: int, worker_id: str) -> bool:
    """Extend the lease of a running job. False when the worker no longer holds it (it was
    requeued or finished elsewhere). Caller commits."""
    res = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.RUNNING, Job.locked_by == worker_id)
        .values(locked_at=utcnow())
    )
    return bool(res.rowcount)


def requeue_stale(session: Session, older_than_minutes: float) -> int:
    """Return the jobs of dead workers to the queue: running jobs whose lease (locked_at) was not
    renewed for `older_than_minutes`."""
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    res = session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, or_(Job.locked_at.is_(None), Job.locked_at < cutoff))
        .values(status=JobStatus.QUEUED, locked_at=None, locked_by=None, run_at=utcnow())
    )
    return res.rowcount or 0


def release_owned(session: Session, worker_prefix: str, *, consume_attempt: bool = False) -> int:
    """Return the running jobs locked by workers whose id starts with `worker_prefix` to the
    queue at once: used at startup (a previous process with the same name is certainly dead) and
    when the shutdown grace period ran out. A job released at shutdown did not fail, so by default
    it keeps its attempt."""
    values: dict[str, object] = {
        "status": JobStatus.QUEUED,
        "locked_at": None,
        "locked_by": None,
        "run_at": utcnow(),
    }
    if not consume_attempt:
        values["attempts"] = func.greatest(Job.attempts - 1, 0)
    res = session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_by.startswith(worker_prefix, autoescape=True))
        .values(**values)
    )
    return res.rowcount or 0


def enqueue_or_move(
    session: Session,
    kind: str,
    payload: dict,
    *,
    dedupe_key: str,
    run_at: datetime,
    max_attempts: int = 5,
    only_earlier: bool = False,
) -> int | None:
    """Make sure a job of this kind will run at `run_at`: queue it, or move the queued job with
    the same dedupe key (only earlier when `only_earlier`).

    A RUNNING job with the key cannot be moved and will not see what the caller just changed
    (e.g. a post the user pushed out now while publish_post was still sending its
    notification), so a second job is queued under f"{dedupe_key}:next". Handlers must be safe to
    run twice. Returns the id of the job that will run, or None."""
    for key in (dedupe_key, f"{dedupe_key}:next"):
        job_id = enqueue(session, kind, payload, run_at=run_at, dedupe_key=key, max_attempts=max_attempts)
        if job_id is not None:
            return job_id
        new_run_at = func.least(Job.run_at, run_at) if only_earlier else run_at
        moved = session.execute(
            update(Job)
            .where(Job.dedupe_key == key, Job.status == JobStatus.QUEUED)
            .values(run_at=new_run_at)
            .returning(Job.id)
        ).scalar_one_or_none()
        if moved is not None:
            return int(moved)
    return None


def purge_finished(session: Session, older_than_days: int = 14) -> int:
    from sqlalchemy import delete

    cutoff = utcnow() - timedelta(days=older_than_days)
    res = session.execute(
        delete(Job).where(Job.status.in_([JobStatus.DONE, JobStatus.DEAD]), Job.finished_at < cutoff)
    )
    return res.rowcount or 0
