"""All persistent entities. Every tenant-owned table carries workspace_id.

Owned by the core. Feature modules must not edit this file; request schema
changes instead so the migration stays consistent.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repeater.db import Base


class Platform(enum.StrEnum):
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    GDRIVE = "gdrive"  # source-only (Google Drive folder)


PUBLISH_PLATFORMS = (Platform.TIKTOK, Platform.YOUTUBE, Platform.INSTAGRAM, Platform.FACEBOOK)

# OAuth provider that issues the tokens for each platform.
PROVIDER_FOR_PLATFORM = {
    Platform.TIKTOK: "tiktok",
    Platform.YOUTUBE: "google",
    Platform.GDRIVE: "google",
    Platform.INSTAGRAM: "meta",
    Platform.FACEBOOK: "meta",
}


class AccountStatus(enum.StrEnum):
    ACTIVE = "active"
    NEEDS_RECONNECT = "needs_reconnect"
    DISABLED = "disabled"


class SourceKind(enum.StrEnum):
    PLATFORM_API = "platform_api"  # official API listing + download (Instagram, Facebook)
    YTDLP = "ytdlp"  # unofficial download (TikTok, YouTube); opt-in, isolated
    DRIVE = "drive"  # Google Drive folder
    MANUAL = "manual"  # uploads from the dashboard


class VideoStatus(enum.StrEnum):
    IMPORTING = "importing"  # row exists, file not yet downloaded
    PROCESSING = "processing"  # downloaded, probing/encoding
    READY = "ready"  # encoded and eligible for rotation
    DUPLICATE = "duplicate"  # same content as duplicate_of_id; never rotated
    FAILED = "failed"
    ARCHIVED = "archived"  # user removed it from rotation


class PostStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    UPLOADING = "uploading"
    PROCESSING = "processing"  # uploaded; waiting for the platform to confirm
    PUBLISHED = "published"
    FAILED = "failed"  # attempt failed; a retry is queued
    NEEDS_ATTENTION = "needs_attention"  # retries exhausted or needs the user
    SKIPPED = "skipped"
    CANCELED = "canceled"


ACTIVE_POST_STATUSES = (
    PostStatus.SCHEDULED,
    PostStatus.UPLOADING,
    PostStatus.PROCESSING,
    PostStatus.FAILED,
)


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


class ActivityLevel(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x], native_enum=False, length=32)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Cairo")
    locale: Mapped[str] = mapped_column(String(8), default="ar")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    # Minimum gap between publishing the same video on two different platforms.
    cross_platform_gap_minutes: Mapped[int] = mapped_column(Integer, default=24 * 60)
    # How far ahead the scheduler fills the calendar.
    horizon_hours: Mapped[int] = mapped_column(Integer, default=48)

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    locale: Mapped[str] = mapped_column(String(8), default="ar")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Acceptance of the Terms of Service and Privacy Policy (YouTube Developer Policies III.A.2):
    # when, and which version of the legal texts (repeater.web.consent.LEGAL_VERSION).
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_version: Mapped[str | None] = mapped_column(String(32))

    memberships: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class WorkspaceInvitation(Base):
    __tablename__ = "workspace_invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    password_version: Mapped[str] = mapped_column(String(64))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecoveryRequest(Base):
    __tablename__ = "recovery_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class AccessThrottle(Base):
    __tablename__ = "access_throttles"

    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="owner")  # owner | editor

    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class CreatorProfile(TimestampMixin, Base):
    """One person or brand, with an isolated content library inside a workspace."""

    __tablename__ = "creator_profiles"
    __table_args__ = (UniqueConstraint("id", "workspace_id", name="uq_profile_workspace"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))


class ContentPlan(TimestampMixin, Base):
    """Saved planning intent only. Never consumed by the publishing worker."""

    __tablename__ = "content_plans"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_plan_workspace"),
        UniqueConstraint("profile_id", name="uq_plan_profile"),
        ForeignKeyConstraint(
            ["profile_id", "workspace_id"],
            ["creator_profiles.id", "creator_profiles.workspace_id"],
            name="fk_plan_profile",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    timezone: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[date] = mapped_column(Date)
    weekdays: Mapped[list] = mapped_column(JSONB, default=lambda: list(range(7)))
    times: Mapped[list] = mapped_column(JSONB, default=lambda: ["10:00"])
    ordering: Mapped[str] = mapped_column(String(16), default="oldest")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class PlanTarget(Base):
    __tablename__ = "plan_targets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "workspace_id"],
            ["content_plans.id", "content_plans.workspace_id"],
            name="fk_target_plan",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["connected_accounts.id", "connected_accounts.workspace_id"],
            name="fk_target_account",
            ondelete="CASCADE",
        ),
    )

    plan_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer)


class ConnectedAccount(TimestampMixin, Base):
    """An account on a platform authorized via OAuth (a TikTok creator, a YouTube channel,
    an Instagram professional account, a Facebook Page, or a Google Drive login)."""

    __tablename__ = "connected_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "external_id", name="uq_account_external"),
        UniqueConstraint("id", "workspace_id", name="uq_account_workspace"),
        ForeignKeyConstraint(
            ["profile_id", "workspace_id"],
            ["creator_profiles.id", "creator_profiles.workspace_id"],
            name="fk_account_profile",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[int | None] = mapped_column(Integer, index=True)
    platform: Mapped[Platform] = mapped_column(_enum(Platform, "platform"))
    external_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(255), default="")
    avatar_url: Mapped[str | None] = mapped_column(Text)
    # Encrypted JSON of OAuthTokens (see repeater.crypto.encrypt_json). Never logged.
    token_enc: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str] = mapped_column(Text, default="")
    # Platform-specific non-secret data, e.g. {"page_id": ..., "ig_user_id": ...}.
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[AccountStatus] = mapped_column(
        _enum(AccountStatus, "account_status"), default=AccountStatus.ACTIVE
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_info: Mapped[dict | None] = mapped_column(JSONB)


class Source(TimestampMixin, Base):
    """Where new videos come from."""

    __tablename__ = "sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "workspace_id"],
            ["creator_profiles.id", "creator_profiles.workspace_id"],
            name="fk_source_profile",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"))
    # Platform the videos originate from (tiktok/youtube/instagram/facebook/gdrive); None for manual.
    platform: Mapped[Platform | None] = mapped_column(_enum(Platform, "source_platform"))
    connected_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("connected_accounts.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="")
    # Kind-specific config, e.g. {"profile_url": ...}, {"folder_id": ...}, {"backfill_count": 0}.
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Explicit user acknowledgement for the unofficial (yt-dlp) path.
    unofficial_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_info: Mapped[dict | None] = mapped_column(JSONB)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    connected_account: Mapped[ConnectedAccount | None] = relationship()


class Video(TimestampMixin, Base):
    __tablename__ = "videos"
    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "workspace_id"],
            ["creator_profiles.id", "creator_profiles.workspace_id"],
            name="fk_video_profile",
        ),
        Index(
            "uq_video_source_external",
            "workspace_id",
            text("COALESCE(profile_id, 0)"),
            "source_platform",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("ix_video_ws_status", "workspace_id", "status"),
        Index("ix_video_sha256", "workspace_id", "sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    profile_id: Mapped[int | None] = mapped_column(Integer, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    source_platform: Mapped[Platform | None] = mapped_column(_enum(Platform, "video_source_platform"))
    # The account the video came from (so it is never re-posted to that same account).
    source_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("connected_accounts.id", ondelete="SET NULL")
    )
    # Minimal deletion lineage for imports kept after the disconnected account is purged.
    # It leaves with the video and lets a later Meta callback still find that user's data.
    meta_deletion_user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128))
    permalink: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    duration_s: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    original_path: Mapped[str | None] = mapped_column(Text)  # relative to media_root
    encoded_path: Mapped[str | None] = mapped_column(Text)  # relative to media_root
    encoded_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    thumbnail_path: Mapped[str | None] = mapped_column(Text)  # relative to media_root
    sha256: Mapped[str | None] = mapped_column(String(64))
    # Perceptual fingerprint: space-separated hex pHash of sampled frames.
    phash: Mapped[str | None] = mapped_column(Text)

    status: Mapped[VideoStatus] = mapped_column(
        _enum(VideoStatus, "video_status"), default=VideoStatus.IMPORTING
    )
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    error_info: Mapped[dict | None] = mapped_column(JSONB)
    # Per-video overrides, e.g. {"exclude_platforms": ["facebook"], "title": "..."}.
    overrides: Mapped[dict] = mapped_column(JSONB, default=dict)

    source: Mapped[Source | None] = relationship()


class RotationRule(TimestampMixin, Base):
    """Publishing rule for one target account."""

    __tablename__ = "rotation_rules"
    __table_args__ = (UniqueConstraint("workspace_id", "connected_account_id", name="uq_rule_account"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    connected_account_id: Mapped[int] = mapped_column(ForeignKey("connected_accounts.id", ondelete="CASCADE"))
    platform: Mapped[Platform] = mapped_column(_enum(Platform, "rule_platform"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_cap: Mapped[int] = mapped_column(Integer, default=2)
    min_gap_minutes: Mapped[int] = mapped_column(Integer, default=240)
    window_start: Mapped[time] = mapped_column(Time, default=time(10, 0))
    window_end: Mapped[time] = mapped_column(Time, default=time(22, 0))
    # Template with {title} {caption} {hashtags} {source_caption}.
    caption_template: Mapped[str] = mapped_column(Text, default="{caption}")
    hashtags: Mapped[str] = mapped_column(Text, default="")
    # Platform publish settings approved once by the user, e.g. TikTok privacy_level and toggles,
    # YouTube privacy_status/made_for_kids/category_id, Instagram share_to_feed.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Evergreen: re-publish to this account after N days (None = never).
    evergreen_days: Mapped[int | None] = mapped_column(Integer)
    # Only rotate videos imported after this moment (None = whole library).
    only_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    connected_account: Mapped[ConnectedAccount] = relationship()


class ScheduledPost(TimestampMixin, Base):
    __tablename__ = "scheduled_posts"
    __table_args__ = (
        Index("ix_post_ws_sched", "workspace_id", "scheduled_at"),
        Index("ix_post_account_sched", "connected_account_id", "scheduled_at"),
        Index("ix_post_video", "video_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    connected_account_id: Mapped[int] = mapped_column(ForeignKey("connected_accounts.id", ondelete="CASCADE"))
    platform: Mapped[Platform] = mapped_column(_enum(Platform, "post_platform"))
    # 0 = first publish; 1, 2, ... = evergreen re-publishes.
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    # "{video_id}:{account_id}:{cycle}" — guarantees one publish per video/account/cycle.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[PostStatus] = mapped_column(_enum(PostStatus, "post_status"), default=PostStatus.SCHEDULED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    caption: Mapped[str | None] = mapped_column(Text)  # rendered at publish time
    # Adapter state between publish() and poll(): container ids, upload ids, etc.
    publish_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    platform_post_id: Mapped[str | None] = mapped_column(String(255))
    permalink: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    error_info: Mapped[dict | None] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(32), default="scheduler")  # scheduler | user

    video: Mapped[Video] = relationship()
    connected_account: Mapped[ConnectedAccount] = relationship()


class YouTubeQuotaDay(Base):
    """Project-wide circuit and serialization point for one Pacific quota day."""

    __tablename__ = "youtube_quota_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    exhausted: Mapped[bool] = mapped_column(Boolean, default=False)


class YouTubeUploadStart(Base):
    """One reserved videos.insert request. Retry/reset/deletion cannot refund it."""

    __tablename__ = "youtube_upload_starts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(ForeignKey("youtube_quota_days.day", ondelete="CASCADE"), index=True)
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_posts.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Job(Base):
    """Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_job_ready", "status", "run_at"),
        Index(
            "uq_job_dedupe_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus, "job_status"), default=JobStatus.QUEUED)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivityLog(Base):
    """User-visible event log (imports, publishes, errors)."""

    __tablename__ = "activity_log"
    __table_args__ = (Index("ix_activity_ws_created", "workspace_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    level: Mapped[ActivityLevel] = mapped_column(
        _enum(ActivityLevel, "activity_level"), default=ActivityLevel.INFO
    )
    category: Mapped[str] = mapped_column(String(32))  # import | publish | account | source | system | auth
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthState(Base):
    """Short-lived OAuth state + PKCE verifier for the connect flow."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    # Which platform the user clicked (e.g. "youtube" vs "gdrive" share the Google provider).
    purpose: Mapped[str] = mapped_column(String(32))
    return_path: Mapped[str | None] = mapped_column(String(200))
    code_verifier: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataDeletionRequest(Base):
    """Meta data-deletion callback records (status page for the user)."""

    __tablename__ = "data_deletion_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    external_user_id: Mapped[str] = mapped_column(String(128))
    confirmation_code: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="received")  # received | completed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _capture_error_message(target, value, oldvalue, initiator):
    # Keep the English diagnostic and its translation metadata in the same transaction.
    # Assigning None/plain legacy text also clears any earlier key, avoiding stale translations.
    from repeater.messages import Message

    setattr(target, initiator.key + "_info", value.info if isinstance(value, Message) else None)


for _model, _field in (
    (ConnectedAccount, "last_error"),
    (Source, "last_error"),
    (Video, "error"),
    (ScheduledPost, "error"),
):
    event.listen(getattr(_model, _field), "set", _capture_error_message)
