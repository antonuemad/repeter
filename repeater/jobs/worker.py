"""Background worker: python -m repeater.jobs.worker [--threads N]

N worker threads (settings.worker_threads) claim and run jobs. Each job is claimed in its own
short transaction, its handler runs in a fresh session (committed when the handler returns), and
the job is marked done / rescheduled / failed in a third transaction:
    handler returns      -> done
    RetryLater raised    -> queue.reschedule (no attempt consumed)
    any other exception  -> queue.fail with queue.default_backoff (dead after max_attempts)

A ticker thread runs every TICK_SECONDS and enqueues the periodic work; dedupe keys make it
idempotent across threads and processes:
    requeued_jobs    running jobs whose lease lapsed (their worker died) go back to the queue;
                     also done once when the worker starts
    poll_source      enabled non-manual sources whose poll interval elapsed
    plan_workspace   every 5 minutes per non-paused workspace
    refresh_tokens   every 30 minutes
    maintenance      hourly

Leases: while a handler runs, a helper thread renews the job's locked_at every
settings.job_lease_refresh_seconds, so a job is only requeued when its worker is gone (see
queue.py). Jobs are claimed by priority class first (publishing before imports), and with two or
more threads the first one never takes bulk jobs (imports, encodes), so a long encode on every
other thread cannot hold up a due publish_post or poll_post.

Every loop touches settings.worker_heartbeat_path (the container healthcheck reads its age).
SIGTERM/SIGINT stop claiming new jobs; jobs already running get settings.worker_shutdown_grace_s
to finish (the compose stop_grace_period must exceed it); jobs still running after that are
returned to the queue at once. Logs are JSON lines on stdout. run_once() and tick() drive single
iterations deterministically in tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from repeater import queue
from repeater.config import get_settings
from repeater.jobs import kinds
from repeater.jobs.registry import Handler, load_handlers
from repeater.models import Job, JobStatus, Workspace
from repeater.platforms.base import PlatformError
from repeater.queue import RetryLater
from repeater.timeutil import utcnow

log = logging.getLogger("repeater.worker")

SessionFactory = Callable[[], Session]

TICK_SECONDS = 45.0
PLAN_EVERY = timedelta(minutes=5)
REFRESH_EVERY = timedelta(minutes=30)
MAINTENANCE_EVERY = timedelta(hours=1)
MAX_ERROR_BACKOFF_SECONDS = 60.0
# Waits before the 2nd, 3rd and 4th try of recording a job's outcome after a database error.
FINISH_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0, 8.0)
HEARTBEAT_SLICE_SECONDS = 5.0

_URL_RE = re.compile(r"https?://\S+")
_SECRET_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|api_key|token|secret|password|authorization)"
    r"(\s*[=:]\s*)\S+"
)


@dataclass(frozen=True)
class _Outcome:
    action: str  # done | later | failed
    seconds: float = 0.0
    message: str = ""
    retry: bool = True


# =========================================================================== single iterations


def run_once(
    session_factory: SessionFactory,
    worker_id: str,
    *,
    handlers: dict[str, Handler] | None = None,
    job_kinds: list[str] | None = None,
    lease_seconds: float | None = None,
) -> bool:
    """Claim and run at most one due job. Returns True if a job was processed. The job's lease
    is renewed every `lease_seconds` (default settings.job_lease_refresh_seconds) while it runs."""
    handlers = load_handlers() if handlers is None else handlers
    claimed = _claim(session_factory, worker_id, job_kinds)
    if claimed is None:
        return False
    job_id, kind, payload = claimed
    started = time.monotonic()
    interval = get_settings().job_lease_refresh_seconds if lease_seconds is None else lease_seconds
    with _Lease(session_factory, job_id, worker_id, interval):
        outcome = _execute(session_factory, handlers.get(kind), job_id, kind, payload)
    _finish_with_retry(session_factory, job_id, worker_id, outcome)
    log.info(
        "job %s %s: %s",
        job_id,
        kind,
        outcome.action,
        extra={
            "job_id": job_id,
            "kind": kind,
            "outcome": outcome.action,
            "seconds": round(time.monotonic() - started, 3),
        },
    )
    return True


def tick(session_factory: SessionFactory, now: datetime | None = None) -> dict[str, int]:
    """Requeue abandoned jobs and enqueue the periodic jobs. Returns how many jobs each step
    requeued or queued (-1 = step failed)."""
    now = now or utcnow()
    steps: list[tuple[str, Callable[[Session], int]]] = [
        ("requeued_jobs", lambda s: queue.requeue_stale(s, queue.lease_timeout_minutes())),
        ("poll_source", lambda s: _enqueue_due_polls(s, now)),
        ("plan_workspace", lambda s: _enqueue_plans(s, now)),
        (
            "refresh_tokens",
            lambda s: _enqueue_periodic(s, kinds.REFRESH_TOKENS, next_boundary(now, REFRESH_EVERY)),
        ),
        (
            "maintenance",
            lambda s: _enqueue_periodic(s, kinds.MAINTENANCE, next_boundary(now, MAINTENANCE_EVERY)),
        ),
    ]
    if get_settings().mail_configured:
        steps.append(
            (
                "deliver_mail",
                lambda s: _enqueue_periodic(s, "deliver_mail", next_boundary(now, timedelta(minutes=1))),
            )
        )
    counts: dict[str, int] = {}
    with session_factory() as session:
        for name, step in steps:
            try:
                with session.begin_nested():
                    counts[name] = int(step(session))
            except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
                log.error("tick step %s failed: %s", name, safe_error(exc))
                counts[name] = -1
        session.commit()
    return counts


def next_boundary(now: datetime, period: timedelta) -> datetime:
    """The first multiple of `period` (counted from the Unix epoch) strictly after `now`."""
    step = period.total_seconds()
    return datetime.fromtimestamp((now.timestamp() // step + 1) * step, UTC)


def touch_heartbeat(path: Path | str | None) -> None:
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        os.utime(p, None)
    except OSError as exc:
        log.warning("cannot touch heartbeat file (%s)", type(exc).__name__)


def safe_error(exc: BaseException) -> str:
    """Exception text for logs and job rows, with URLs and token-like values removed."""
    text = exc.message if isinstance(exc, PlatformError) else str(exc)
    text = _SECRET_RE.sub(r"\1\2<redacted>", _URL_RE.sub("<url>", text)).strip()
    name = type(exc).__name__
    return f"{name}: {text}"[:2000] if text else name


# =========================================================================== job lifecycle


def _claim(
    session_factory: SessionFactory, worker_id: str, job_kinds: list[str] | None
) -> tuple[int, str, dict[str, Any]] | None:
    with session_factory() as session:
        job = queue.claim_next(session, worker_id, job_kinds)
        if job is None:
            session.rollback()
            return None
        claimed = (int(job.id), job.kind, dict(job.payload or {}))
        session.commit()
        return claimed


def _execute(
    session_factory: SessionFactory, fn: Handler | None, job_id: int, kind: str, payload: dict[str, Any]
) -> _Outcome:
    if fn is None:
        return _Outcome("failed", message=f"no handler registered for job kind {kind!r}", retry=False)
    try:
        with session_factory() as session:
            fn(session, payload)
            session.commit()
    except RetryLater as again:
        return _Outcome("later", seconds=max(float(again.seconds), 0.0), message=again.reason)
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        message = safe_error(exc)
        log.error(
            "job %s %s failed: %s",
            job_id,
            kind,
            message,
            extra={"job_id": job_id, "kind": kind, "stack": "".join(traceback.format_tb(exc.__traceback__))},
        )
        return _Outcome("failed", message=message)
    return _Outcome("done")


class _Lease:
    """Renews a running job's lease from a helper thread until the block exits."""

    def __init__(self, session_factory: SessionFactory, job_id: int, worker_id: str, interval: float) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._worker_id = worker_id
        self._interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Lease:
        if self._interval > 0:
            self._thread = threading.Thread(target=self._run, name=f"lease-{self._job_id}", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._interval, 5.0))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._session_factory() as session:
                    held = queue.renew_lease(session, self._job_id, self._worker_id)
                    session.commit()
            except Exception as exc:  # noqa: BLE001 - a failed renewal must never kill the job
                log.warning("lease renewal for job %s failed: %s", self._job_id, safe_error(exc))
                continue
            if not held:
                log.warning("job %s lost its lease (requeued elsewhere)", self._job_id)
                return


def _finish_with_retry(
    session_factory: SessionFactory, job_id: int, worker_id: str, outcome: _Outcome
) -> None:
    """Record the outcome, retrying after database errors: a job left RUNNING would only come back
    once its lease lapsed, and then run again."""
    for delay in (*FINISH_RETRY_DELAYS, None):
        try:
            _finish(session_factory, job_id, worker_id, outcome)
            return
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            if delay is None:
                raise
            log.warning(
                "recording the outcome of job %s failed (%s); retrying in %.1f s",
                job_id,
                safe_error(exc),
                delay,
            )
            time.sleep(delay)


def _finish(session_factory: SessionFactory, job_id: int, worker_id: str, outcome: _Outcome) -> None:
    with session_factory() as session:
        job = session.get(Job, job_id, with_for_update=True)
        if job is None or job.status != JobStatus.RUNNING or job.locked_by != worker_id:
            # Requeued by requeue_stale while it ran: whoever holds it now decides.
            log.warning("job %s is no longer held by %s; leaving it as is", job_id, worker_id)
            session.rollback()
            return
        if outcome.action == "done":
            queue.complete(session, job)
        elif outcome.action == "later":
            queue.reschedule(session, job, outcome.seconds, outcome.message)
        else:
            retry_in = queue.default_backoff(job.attempts) if outcome.retry else None
            queue.fail(session, job, outcome.message, retry_in=retry_in)
        session.commit()


# =========================================================================== periodic work


def _enqueue_due_polls(session: Session, now: datetime) -> int:
    from repeater.sources import importer

    return importer.enqueue_due_polls(session, now)


def _enqueue_plans(session: Session, now: datetime) -> int:
    if not get_settings().publishing_enabled:
        return 0
    run_at = next_boundary(now, PLAN_EVERY)
    ids = session.execute(
        select(Workspace.id).where(Workspace.paused.is_(False)).order_by(Workspace.id)
    ).scalars()
    return sum(kinds.enqueue_plan_workspace(session, ws_id, run_at=run_at) is not None for ws_id in ids)


def _enqueue_periodic(session: Session, kind: str, run_at: datetime) -> int:
    return int(queue.enqueue(session, kind, {}, run_at=run_at, dedupe_key=kind, max_attempts=1) is not None)


# =========================================================================== threads


class Worker:
    """Job threads plus the ticker. start() returns at once; stop() asks threads to finish."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        threads: int,
        poll_seconds: float,
        heartbeat_path: Path | str | None,
        handlers: dict[str, Handler] | None = None,
        tick_seconds: float = TICK_SECONDS,
        name: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.threads = max(1, int(threads))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.heartbeat_path = heartbeat_path
        self.handlers = load_handlers() if handlers is None else handlers
        self.tick_seconds = tick_seconds
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def urgent_kinds(self) -> list[str] | None:
        """The kinds the reserved thread handles: everything but bulk work. None when the worker
        has a single thread (it takes everything, urgent first) or no urgent handler."""
        if self.threads < 2:
            return None
        urgent = [
            kind
            for kind in self.handlers
            if queue.KIND_PRIORITY.get(kind, queue.DEFAULT_PRIORITY) < queue.PRIORITY_BULK
        ]
        return urgent or None

    def start(self) -> None:
        self._recover_after_restart()
        urgent = self.urgent_kinds()
        for i in range(self.threads):
            worker_id = f"{self.name}:{i}"
            self._threads.append(
                threading.Thread(
                    target=self._work_loop,
                    args=(worker_id, urgent if i == 0 else None),
                    name=f"worker-{i}",
                    daemon=True,
                )
            )
        self._threads.append(threading.Thread(target=self._tick_loop, name="ticker", daemon=True))
        for t in self._threads:
            t.start()
        log.info("worker started with %s job threads", self.threads)

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait up to `timeout` seconds in total (None = until done) for the threads to stop,
        keeping the heartbeat fresh meanwhile. Returns True when every thread has stopped."""
        deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0.0)
        while True:
            alive = [t for t in self._threads if t.is_alive()]
            if not alive:
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            slice_ = HEARTBEAT_SLICE_SECONDS if remaining is None else min(HEARTBEAT_SLICE_SECONDS, remaining)
            alive[0].join(slice_)
            touch_heartbeat(self.heartbeat_path)

    def release_in_flight(self) -> int:
        """Return the jobs this process still runs to the queue (the shutdown grace ran out).
        Their handlers may still be running; the process exits right after."""
        try:
            with self.session_factory() as session:
                released = queue.release_owned(session, f"{self.name}:")
                session.commit()
        except Exception as exc:  # noqa: BLE001 - the lease lapse recovers them later
            log.error("could not release running jobs: %s", safe_error(exc))
            return 0
        return released

    def _recover_after_restart(self) -> None:
        """Requeue what a crashed predecessor left running: jobs whose lease lapsed and jobs locked
        by a previous process with this worker's name (same host and pid, so certainly dead)."""
        try:
            with self.session_factory() as session:
                stale = queue.requeue_stale(session, queue.lease_timeout_minutes())
                own = queue.release_owned(session, f"{self.name}:", consume_attempt=True)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - the ticker retries every tick
            log.error("startup job recovery failed: %s", safe_error(exc))
            return
        if stale or own:
            log.info("requeued %s abandoned job(s) at startup", stale + own, extra={"jobs": stale + own})

    def _work_loop(self, worker_id: str, job_kinds: list[str] | None = None) -> None:
        errors = 0
        while not self.stop_event.is_set():
            try:
                processed = run_once(
                    self.session_factory, worker_id, handlers=self.handlers, job_kinds=job_kinds
                )
                errors = 0
            except Exception as exc:  # noqa: BLE001 - e.g. database unavailable; keep the thread alive
                errors += 1
                log.error("worker loop error: %s", safe_error(exc))
                self.stop_event.wait(min(MAX_ERROR_BACKOFF_SECONDS, 2.0**errors))
                continue
            touch_heartbeat(self.heartbeat_path)
            if not processed:
                self.stop_event.wait(self.poll_seconds)
        log.info("job thread %s stopped", worker_id)

    def _tick_loop(self) -> None:
        log.info("ticker started (every %.0f s)", self.tick_seconds)
        while not self.stop_event.is_set():
            try:
                counts = tick(self.session_factory)
                queued = {name: n for name, n in counts.items() if n > 0}
                if queued:
                    log.info("ticker queued periodic jobs: %s", queued, extra={"queued": queued})
            except Exception as exc:  # noqa: BLE001 - keep ticking
                log.error("ticker error: %s", safe_error(exc))
            touch_heartbeat(self.heartbeat_path)
            self.stop_event.wait(self.tick_seconds)


# =========================================================================== logging & main


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Exception messages are left out (they may embed URLs with
    credentials); the exception type and stack are kept."""

    _RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                data[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            data["exc_type"] = record.exc_info[0].__name__
            data["stack"] = "".join(traceback.format_tb(record.exc_info[2]))
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO; some carry credentials (e.g. Telegram bot URLs).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    from repeater.config import get_settings
    from repeater.db import get_sessionmaker

    parser = argparse.ArgumentParser(prog="repeater-worker")
    parser.add_argument("--threads", type=int, default=None, help="job threads (default: settings)")
    parser.add_argument("--log-level", default=os.environ.get("REPEATER_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    configure_logging(args.log_level.upper())
    settings = get_settings()
    worker = Worker(
        get_sessionmaker(),
        threads=args.threads or settings.worker_threads,
        poll_seconds=settings.worker_poll_seconds,
        heartbeat_path=settings.worker_heartbeat_path,
    )

    def _stop(signum: int, _frame: object) -> None:
        log.info("received %s; finishing running jobs", signal.Signals(signum).name)
        worker.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    touch_heartbeat(settings.worker_heartbeat_path)
    worker.start()
    while not worker.stop_event.wait(1.0):
        pass
    grace = settings.worker_shutdown_grace_s
    if worker.join(grace):
        log.info("worker stopped")
    else:
        released = worker.release_in_flight()
        log.warning(
            "shutdown grace of %s s ran out; %s running job(s) returned to the queue",
            grace,
            released,
            extra={"released": released},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
