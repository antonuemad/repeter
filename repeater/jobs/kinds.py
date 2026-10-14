"""Job kinds, payloads and owners (the contract between modules), plus enqueue helpers.

kind              payload                         dedupe key              handled in
----------------  ------------------------------  ----------------------  ----------------------
poll_source       {"source_id": int}              poll_source:{id}        repeater.sources.jobs
import_video      {"video_id": int}               import_video:{id}       repeater.sources.jobs
process_video     {"video_id": int}               process_video:{id}      repeater.sources.jobs
plan_workspace    {"workspace_id": int}           plan_workspace:{id}     repeater.engine.jobs
publish_post      {"post_id": int}                publish_post:{id}       repeater.engine.jobs
poll_post         {"post_id": int}                poll_post:{id}          repeater.engine.jobs
refresh_tokens    {}                              refresh_tokens          repeater.engine.jobs
maintenance       {}                              maintenance             repeater.jobs.maintenance
delete_user_data  {"request_id": int}             delete_user_data:{id}   repeater.engine.jobs

Claim priority (repeater.queue.KIND_PRIORITY): publish_post and poll_post first, then
plan_workspace, refresh_tokens, maintenance and delete_user_data, then poll_source, and
import_video and process_video last, so a backfill of imports never delays a due publish.

Optional payload keys written by the importer (repeater.sources.importer):
    import_video   "download_url": str, "remote_extra": dict  listing details for the download
                   (short-lived CDN URLs that have no column), "retries": int
    process_video  "retries": int
"retries" counts failed tries; the handlers keep it because RetryLater does not consume queue
attempts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from repeater.queue import enqueue, enqueue_or_move
from repeater.timeutil import utcnow

POLL_SOURCE = "poll_source"
IMPORT_VIDEO = "import_video"
PROCESS_VIDEO = "process_video"
PLAN_WORKSPACE = "plan_workspace"
PUBLISH_POST = "publish_post"
POLL_POST = "poll_post"
REFRESH_TOKENS = "refresh_tokens"
MAINTENANCE = "maintenance"
DELETE_USER_DATA = "delete_user_data"


def enqueue_poll_source(session: Session, source_id: int, run_at: datetime | None = None) -> int | None:
    return enqueue(
        session,
        POLL_SOURCE,
        {"source_id": source_id},
        run_at=run_at,
        dedupe_key=f"{POLL_SOURCE}:{source_id}",
        max_attempts=1,
    )


def enqueue_import_video(session: Session, video_id: int) -> int | None:
    return enqueue(
        session, IMPORT_VIDEO, {"video_id": video_id}, dedupe_key=f"{IMPORT_VIDEO}:{video_id}", max_attempts=4
    )


def enqueue_process_video(session: Session, video_id: int) -> int | None:
    return enqueue(
        session,
        PROCESS_VIDEO,
        {"video_id": video_id},
        dedupe_key=f"{PROCESS_VIDEO}:{video_id}",
        max_attempts=3,
    )


def enqueue_plan_workspace(session: Session, workspace_id: int, run_at: datetime | None = None) -> int | None:
    """Queue a plan. With an explicit `run_at` (the periodic tick) an active job with the same
    dedupe key means "already planned for later": None is returned. Without one the caller has
    just changed something the plan depends on and wants it applied soon: see
    request_plan_workspace (an already queued job is moved up, and a plan job that is running right
    now is followed by a second one)."""
    if run_at is None:
        return request_plan_workspace(session, workspace_id)
    return enqueue(
        session,
        PLAN_WORKSPACE,
        {"workspace_id": workspace_id},
        run_at=run_at,
        dedupe_key=f"{PLAN_WORKSPACE}:{workspace_id}",
        max_attempts=1,
    )


def request_plan_workspace(session: Session, workspace_id: int, run_at: datetime | None = None) -> int | None:
    """Have the workspace planned as soon as possible (or at run_at): queues the job, or moves the
    queued one earlier. When a plan job is running right now it started before the caller's change
    and cannot see it, so a second job is queued behind it."""
    return enqueue_or_move(
        session,
        PLAN_WORKSPACE,
        {"workspace_id": workspace_id},
        dedupe_key=f"{PLAN_WORKSPACE}:{workspace_id}",
        run_at=run_at or utcnow(),
        max_attempts=1,
        only_earlier=True,
    )


def enqueue_publish_post(session: Session, post_id: int, run_at: datetime | None = None) -> int | None:
    # Retries are driven by the engine (scheduled_posts.attempts), not by the queue.
    return enqueue(
        session,
        PUBLISH_POST,
        {"post_id": post_id},
        run_at=run_at,
        dedupe_key=f"{PUBLISH_POST}:{post_id}",
        max_attempts=1,
    )


def enqueue_poll_post(session: Session, post_id: int, run_at: datetime | None = None) -> int | None:
    return enqueue(
        session,
        POLL_POST,
        {"post_id": post_id},
        run_at=run_at,
        dedupe_key=f"{POLL_POST}:{post_id}",
        max_attempts=1,
    )


def enqueue_delete_user_data(session: Session, request_id: int) -> int | None:
    return enqueue(
        session,
        DELETE_USER_DATA,
        {"request_id": request_id},
        dedupe_key=f"{DELETE_USER_DATA}:{request_id}",
        max_attempts=5,
    )
