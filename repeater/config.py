"""Application settings, read from environment variables prefixed with REPEATER_."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REPEATER_", env_file=".env", extra="ignore")

    env: str = "development"  # development | production | test
    # Public HTTPS origin of the dashboard and media URLs, e.g. https://rotator.magic-lap.com
    base_url: str = "http://localhost:8000"
    database_url: str = "postgresql+psycopg://repeater:repeater@localhost:5432/repeater"

    # Secret for sessions, CSRF and signed media URLs. Must be long and random in production.
    secret_key: str = "dev-insecure-secret-key-change-me"  # noqa: S105 - dev default; `check` rejects it in production
    # Key ring for OAuth token encryption: comma-separated "kid:base64url(32 bytes)".
    # The first entry encrypts new data; all entries can decrypt.
    token_encryption_keys: str = ""

    media_root: Path = Path("/srv/repeater/media")
    media_url_ttl_seconds: int = 6 * 3600
    max_upload_mb: int = 500

    default_timezone: str = "Africa/Cairo"
    default_locale: str = "ar"

    # Server-wide safety lock. Connecting accounts and importing media still work; publishing
    # requires explicit operator enablement after the testing period ends.
    publishing_enabled: bool = False

    # Platform credentials (developer apps). Empty means the platform is not configured.
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    meta_graph_version: str = "v26.0"
    # TikTok audit state of the developer app. Until TikTok has audited it, every post made through
    # the API is restricted to private viewing (SELF_ONLY, on a private account), so the rule form
    # offers only that and the adapter refuses any other privacy level. Set true after the audit.
    tiktok_audited: bool = False

    # Notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Alerts go to one operator chat for every workspace. By default they carry only the kind of
    # event, a count and a dashboard link; true also sends titles, account names and platform errors.
    telegram_include_details: bool = False

    # Unofficial import (yt-dlp). Off unless a source opts in.
    ytdlp_proxy: str = ""
    ytdlp_cookies_dir: Path | None = None

    # Worker
    worker_poll_seconds: float = 2.0
    worker_threads: int = 2
    # Lease: a running job renews locked_at every job_lease_refresh_seconds; a job whose lease has
    # not been renewed for this long (never less than a few renewals) is requeued.
    job_lock_timeout_minutes: int = 10
    # Touched by the worker loop; the container healthcheck reads its age.
    worker_heartbeat_path: Path = Path("/tmp/repeater-worker.heartbeat")  # noqa: S108

    # Encoding: timeout = min(encode_timeout_max_s, encode_timeout_base_s + per_media_s * duration)
    encode_preset: str = "medium"
    encode_timeout_base_s: int = 300
    encode_timeout_per_media_s: float = 20.0
    encode_timeout_max_s: int = 1800

    # Worker: how long in-flight jobs may finish after SIGTERM before they are abandoned (the
    # compose stop_grace_period must exceed this), and how often a running job renews its lease.
    worker_shutdown_grace_s: int = 600
    job_lease_refresh_seconds: int = 60

    # YouTube Data API: videos.insert calls per Google Cloud project per day (shared by all users).
    youtube_daily_upload_quota: int = 100

    # Retention
    original_retention_days: int = 30
    platform_data_retention_days: int = 30  # YouTube API policy: refresh or delete within 30 days

    # backup.sh writes "<epoch> <ISO time>" here after each successful backup (docker-compose.yml mounts
    # the directory read-only and sets this); `repeater.cli check` and the dashboard warn when it is stale.
    backup_status_file: Path | None = None

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    session_cookie_name: str = "repeater_session"
    session_max_age_seconds: int = 14 * 24 * 3600

    trusted_hosts: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def oauth_redirect_uri(self, provider: str) -> str:
        return f"{self.base_url.rstrip('/')}/oauth/{provider}/callback"

    def platform_configured(self, provider: str) -> bool:
        return {
            "tiktok": bool(self.tiktok_client_key and self.tiktok_client_secret),
            "google": bool(self.google_client_id and self.google_client_secret),
            "meta": bool(self.meta_app_id and self.meta_app_secret),
            "instagram": bool(self.instagram_app_id and self.instagram_app_secret),
        }.get(provider, False)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
