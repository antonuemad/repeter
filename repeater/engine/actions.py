"""User actions on the schedule. OWNER: engine module (implemented by the engine work).

These are called by the web dashboard. Each function commits nothing; the caller commits.
They raise ValueError with a message that can be shown to the user when the action is not
possible in the current state.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from repeater.activity import log_activity
from repeater.engine import captions
from repeater.engine.publisher import queue_publish
from repeater.engine.scheduler import duplicate_origins, limits_problem, lock_workspace
from repeater.jobs import kinds
from repeater.media import storage
from repeater.models import (
    ACTIVE_POST_STATUSES,
    PUBLISH_PLATFORMS,
    AccountStatus,
    ActivityLevel,
    ConnectedAccount,
    Platform,
    PostStatus,
    ScheduledPost,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.timeutil import utcnow

# Posts that have not reached the platform yet.
NOT_STARTED = (PostStatus.SCHEDULED, PostStatus.FAILED, PostStatus.NEEDS_ATTENTION)
IN_FLIGHT = (PostStatus.UPLOADING, PostStatus.PROCESSING)


def skip_post(session: Session, post: ScheduledPost, *, actor: str) -> None:
    """Mark a scheduled (not yet uploading) post as skipped; the planner will not recreate it
    for the same cycle."""
    session.refresh(post, with_for_update=True)
    if post.status not in NOT_STARTED:
        raise ValueError(f"This post is {post.status} and can no longer be skipped.")
    post.status = PostStatus.SKIPPED
    post.error = None
    _log(session, post, f"Skipped the post of '{_label(session, post)}' to {_where(session, post)}.", actor)


def publish_now(session: Session, post: ScheduledPost, *, actor: str) -> None:
    """Move a scheduled/failed/needs_attention post to now and enqueue publish_post."""
    session.refresh(post, with_for_update=True)
    if post.status not in NOT_STARTED:
        raise ValueError(f"This post is {post.status} and cannot be published again from here.")
    _check_can_publish(session, post.workspace_id, session.get(ConnectedAccount, post.connected_account_id))
    if post.status == PostStatus.NEEDS_ATTENTION:
        post.attempts = 0
    now = utcnow()
    post.status = PostStatus.SCHEDULED
    post.scheduled_at = now
    post.error = None
    queue_publish(session, post.id, now)
    _log(session, post, f"Publishing '{_label(session, post)}' to {_where(session, post)} now.", actor)


def schedule_video_now(
    session: Session, video: Video, account: ConnectedAccount, *, actor: str
) -> ScheduledPost:
    """Create (or reuse) the post for video+account in the current cycle, scheduled now,
    bypassing windows/caps but not platform validation. Enqueue publish_post."""
    if video.workspace_id != account.workspace_id:
        raise ValueError("The video and the account belong to different workspaces.")
    platform = Platform(account.platform)
    if platform not in PUBLISH_PLATFORMS:
        raise ValueError(f"{captions.platform_name(platform)} accounts cannot be published to.")
    _check_can_publish(session, video.workspace_id, account)
    if video.status != VideoStatus.READY or not video.encoded_path:
        raise ValueError("Only videos that are ready (imported and encoded) can be published.")
    if video.source_account_id == account.id or account.id in duplicate_origins(
        session, video.workspace_id, [video.id]
    )[1].get(video.id, ()):
        raise ValueError("This video came from this account; publishing it there again would duplicate it.")
    problem = limits_problem(video, platform)
    if problem:
        raise ValueError(f"{captions.platform_name(platform)} cannot take this video: {problem}.")
    lock_workspace(session, video.workspace_id)
    post = _post_for_now(session, video, account)
    now = utcnow()
    post.platform = platform
    post.status = PostStatus.SCHEDULED
    post.scheduled_at = now
    post.attempts = 0
    post.caption = None
    post.publish_state = {}
    post.platform_post_id = None
    post.permalink = None
    post.error = None
    post.published_at = None
    post.created_by = "user"
    session.flush()
    queue_publish(session, post.id, now)
    _log(
        session,
        post,
        f"Publishing '{captions.video_label(video)}' to {captions.platform_name(platform)} "
        f"({account.display_name}) now.",
        actor,
    )
    return post


def archive_video(session: Session, video: Video, *, actor: str) -> None:
    """Remove a video from rotation and cancel its future posts."""
    if video.status == VideoStatus.ARCHIVED:
        return
    video.status = VideoStatus.ARCHIVED
    canceled = session.execute(
        update(ScheduledPost)
        .where(
            ScheduledPost.video_id == video.id,
            ScheduledPost.status.in_([PostStatus.SCHEDULED, PostStatus.FAILED]),
        )
        .values(status=PostStatus.CANCELED, error="The video was archived.")
        .execution_options(synchronize_session="fetch")
    ).rowcount
    log_activity(
        session,
        video.workspace_id,
        ActivityLevel.INFO,
        "publish",
        f"Archived '{captions.video_label(video)}'"
        + (f" and canceled {canceled} scheduled post{'s' if canceled != 1 else ''}." if canceled else "."),
        actor=actor,
        video_id=video.id,
    )


def restore_video(session: Session, video: Video, *, actor: str) -> None:
    """Return an archived video to rotation (status ready) and trigger planning."""
    if video.status != VideoStatus.ARCHIVED:
        raise ValueError("Only archived videos can be restored.")
    if video.duplicate_of_id is not None:
        raise ValueError(
            "This video is a duplicate of another video in the library and stays out of rotation."
        )
    if _file_exists(video.encoded_path):
        video.status = VideoStatus.READY
        video.error = None
        kinds.enqueue_plan_workspace(session, video.workspace_id)
        message = f"Restored '{captions.video_label(video)}' to the rotation."
    elif _file_exists(video.original_path):
        # Archived before encoding finished: encode it now; the importer plans it when ready.
        video.status = VideoStatus.PROCESSING
        video.error = None
        kinds.enqueue_process_video(session, video.id)
        message = f"Restored '{captions.video_label(video)}'; it will join the rotation once processed."
    else:
        raise ValueError("The video's files are no longer on the server; upload it again.")
    log_activity(
        session, video.workspace_id, ActivityLevel.INFO, "publish", message, actor=actor, video_id=video.id
    )


# --------------------------------------------------------------------------- helpers


def _post_for_now(session: Session, video: Video, account: ConnectedAccount) -> ScheduledPost:
    posts = list(
        session.execute(
            select(ScheduledPost)
            .where(ScheduledPost.video_id == video.id, ScheduledPost.connected_account_id == account.id)
            .order_by(ScheduledPost.cycle)
            .with_for_update()
        ).scalars()
    )
    if any(p.status in IN_FLIGHT for p in posts):
        raise ValueError("This video is being published to this account right now.")
    pending = [p for p in posts if p.status in ACTIVE_POST_STATUSES]
    if pending:
        return pending[-1]
    if not posts:
        cycle = 0
    elif posts[-1].status == PostStatus.PUBLISHED:
        cycle = posts[-1].cycle + 1
    else:
        return posts[-1]
    post = ScheduledPost(
        workspace_id=video.workspace_id,
        video_id=video.id,
        connected_account_id=account.id,
        cycle=cycle,
        idempotency_key=f"{video.id}:{account.id}:{cycle}",
    )
    session.add(post)
    return post


def _check_can_publish(session: Session, workspace_id: int, account: ConnectedAccount | None) -> None:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.paused:
        raise ValueError("Publishing is paused for this workspace; resume it first.")
    if account is None or account.status == AccountStatus.DISABLED:
        raise ValueError("The account is disconnected; connect it again first.")
    if account.status != AccountStatus.ACTIVE:
        raise ValueError(
            f"The {captions.platform_name(account.platform)} account '{account.display_name}' must be "
            "reconnected first."
        )


def _file_exists(rel: str | None) -> bool:
    try:
        path = storage.abs_path(rel)
    except ValueError:
        return False
    return path is not None and path.is_file()


def _label(session: Session, post: ScheduledPost) -> str:
    video = session.get(Video, post.video_id)
    return captions.video_label(video) if video is not None else f"#{post.video_id}"


def _where(session: Session, post: ScheduledPost) -> str:
    account = session.get(ConnectedAccount, post.connected_account_id)
    name = account.display_name if account is not None else f"account #{post.connected_account_id}"
    return f"{captions.platform_name(post.platform)} ({name})"


def _log(session: Session, post: ScheduledPost, message: str, actor: str) -> None:
    log_activity(
        session,
        post.workspace_id,
        ActivityLevel.INFO,
        "publish",
        message,
        actor=actor,
        post_id=post.id,
        video_id=post.video_id,
        account_id=post.connected_account_id,
        platform=str(post.platform),
    )
