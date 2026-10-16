"""Execute saved person schedules through the existing durable publishing queue.

No provider calls here. The server switch, workspace pause and explicit plan activation are
independent gates. One pending use per item/page prevents a delayed upload from shortening
the repeat interval. History is confirmed by the publisher, never by opening a preview.
"""

from datetime import timedelta

from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.engine import rules
from repeater.media.content import problem
from repeater.models import (
    ACTIVE_POST_STATUSES,
    AccountStatus,
    ContentPlan,
    PlanTarget,
    Platform,
    PostStatus,
    ScheduledPost,
    VideoStatus,
)
from repeater.timeutil import ensure_aware


def binding_problem(db, post, video, account, *, polling=False):
    """A person post must remain attached to its enabled, tenant-scoped plan and targets."""
    if post.created_by != "plan" or post.plan_id is None:
        return "Person content must be scheduled through its person's plan."
    plan = db.get(ContentPlan, post.plan_id, populate_existing=True)
    if plan is None or not plan.enabled or plan.workspace_id != post.workspace_id:
        return "The person's plan is stopped or no longer available."
    if (
        video.workspace_id != plan.workspace_id
        or account.workspace_id != plan.workspace_id
        or video.profile_id != plan.profile_id
        or account.profile_id != plan.profile_id
        or account.platform != post.platform
        or not db.scalar(
            select(PlanTarget.plan_id).where(
                PlanTarget.plan_id == plan.id,
                PlanTarget.account_id == account.id,
                PlanTarget.workspace_id == plan.workspace_id,
            )
        )
    ):
        return "The content or destination no longer belongs to this plan."
    if not polling and post.plan_revision != plan.revision:
        return "The plan changed; this slot must be planned again."
    return None


def eligibility_problem(db, post, video, account, *, now):
    """Recheck mutable eligibility and the actual repeat gap immediately before upload."""
    issue = binding_problem(db, post, video, account)
    if issue:
        return issue
    if video.source_account_id == account.id or account.platform.value in (video.overrides or {}).get(
        "exclude_platforms", []
    ):
        return "This destination is excluded for this content."
    issue = problem(video, account)
    if issue:
        return f"This content is not compatible with the destination ({issue})."
    plan = db.get(ContentPlan, post.plan_id)
    others = db.scalars(
        select(ScheduledPost).where(
            ScheduledPost.workspace_id == post.workspace_id,
            ScheduledPost.video_id == video.id,
            ScheduledPost.connected_account_id == account.id,
            ScheduledPost.id != post.id,
        )
    )
    from repeater.engine.publisher import reached_platform

    for other in others:
        if other.status == PostStatus.PUBLISHED:
            last = ensure_aware(other.published_at or other.scheduled_at)
            if plan.rotation_mode != "repeat" or now < last + timedelta(days=plan.repeat_after_days):
                return "The actual publication history does not allow this repeat yet."
        elif (
            other.status in ACTIVE_POST_STATUSES
            or other.status == PostStatus.NEEDS_ATTENTION
            or reached_platform(other.platform_post_id, other.publish_state)
        ):
            return "Another pending or uncertain publication must be resolved first."
    rule = rules.find_rule(db, account)
    if (rule and rules.rule_problems(rule)) or (
        rule is None and account.platform in (Platform.YOUTUBE, Platform.TIKTOK)
    ):
        return "Complete the destination's required publishing settings first."
    return None


def cancel_pending(db, plan):
    """Cancel untouched reservations after an edit/pause; never erase an ambiguous result."""
    from repeater.engine.publisher import reached_platform

    for post in db.scalars(
        select(ScheduledPost)
        .where(
            ScheduledPost.plan_id == plan.id,
            ScheduledPost.workspace_id == plan.workspace_id,
            ScheduledPost.status.in_((PostStatus.SCHEDULED, PostStatus.FAILED)),
        )
        .with_for_update()
    ):
        if not reached_platform(post.platform_post_id, post.publish_state):
            post.status = PostStatus.CANCELED
            post.error = "The person plan changed or was paused; this reservation was canceled."


def plan_people(db, workspace, *, now):
    """Caller holds the workspace advisory lock. Returns newly queued posts, without committing."""
    if not get_settings().publishing_enabled or workspace.paused:
        return []
    from repeater.content_plans import simulate
    from repeater.engine.publisher import queue_publish, reached_platform
    from repeater.engine.scheduler import PLAN_LEAD, YOUTUBE_QUOTA_TZ, _youtube_days, youtube_daily_budget

    plans = list(
        db.scalars(
            select(ContentPlan)
            .where(ContentPlan.workspace_id == workspace.id, ContentPlan.enabled.is_(True))
            .order_by(ContentPlan.id)
            .with_for_update()
        )
    )
    if not plans:
        return []
    # All workspaces share the project's YouTube budget; use the legacy planner's same lock.
    db.execute(select(func.pg_advisory_xact_lock(727002, 0)))
    earliest, horizon = now + PLAN_LEAD, now + timedelta(hours=workspace.horizon_hours)
    budget = _youtube_days(db, now - timedelta(days=1), horizon + timedelta(days=1))
    created = []
    for plan in plans:
        preview = simulate(db, plan, now=earliest)
        accounts = preview["accounts"]
        existing = list(
            db.scalars(
                select(ScheduledPost).where(
                    ScheduledPost.workspace_id == workspace.id,
                    ScheduledPost.connected_account_id.in_([a.id for a in accounts]),
                )
            )
        )
        occupied = set()
        for post in existing:
            if post.plan_id == plan.id and post.plan_revision == plan.revision and post.slot_at:
                occupied.add((post.connected_account_id, ensure_aware(post.slot_at)))
            if post.status in (*ACTIVE_POST_STATUSES, PostStatus.PUBLISHED) or reached_platform(
                post.platform_post_id, post.publish_state
            ):
                occupied.add(
                    (post.connected_account_id, ensure_aware(post.published_at or post.scheduled_at))
                )
        reserved = set()
        for slot in preview["rows"]:
            if slot.at > horizon:
                break
            for entry in slot.entries:
                account, video = entry.account, entry.video
                if (
                    video is None
                    or account.status != AccountStatus.ACTIVE
                    or video.status != VideoStatus.READY
                ):
                    continue
                pair = (video.id, account.id)
                if pair in reserved or (account.id, slot.at) in occupied:
                    continue
                rule = rules.find_rule(db, account)
                if (rule and rules.rule_problems(rule)) or (
                    rule is None and account.platform in (Platform.YOUTUBE, Platform.TIKTOK)
                ):
                    continue
                # Respect retry-shifted reservations and an account's configured minimum gap.
                gap = (
                    timedelta(minutes=max(1, int(rule.min_gap_minutes or 0)))
                    if rule
                    else timedelta(minutes=1)
                )
                if any(aid == account.id and abs(slot.at - at) < gap for aid, at in occupied):
                    continue
                day = slot.at.astimezone(YOUTUBE_QUOTA_TZ).date()
                if account.platform == Platform.YOUTUBE and budget[day] >= youtube_daily_budget():
                    continue
                cycle = 1 + max(
                    (
                        p.cycle
                        for p in existing
                        if p.video_id == video.id and p.connected_account_id == account.id
                    ),
                    default=-1,
                )
                post = ScheduledPost(
                    workspace_id=workspace.id,
                    video_id=video.id,
                    connected_account_id=account.id,
                    platform=account.platform,
                    cycle=cycle,
                    idempotency_key=f"{video.id}:{account.id}:{cycle}",
                    scheduled_at=slot.at,
                    slot_at=slot.at,
                    status=PostStatus.SCHEDULED,
                    created_by="plan",
                    plan_id=plan.id,
                    plan_revision=plan.revision,
                )
                db.add(post)
                db.flush()
                queue_publish(db, post.id, slot.at)
                reserved.add(pair)
                occupied.add((account.id, slot.at))
                existing.append(post)
                created.append(post)
                if account.platform == Platform.YOUTUBE:
                    budget[day] += 1
    return created
