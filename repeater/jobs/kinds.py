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

from repeater.queue import enqueue

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
    return enqueue(
        session,
        PLAN_WORKSPACE,
        {"workspace_id": workspace_id},
        run_at=run_at,
        dedupe_key=f"{PLAN_WORKSPACE}:{workspace_id}",
        max_attempts=1,
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
