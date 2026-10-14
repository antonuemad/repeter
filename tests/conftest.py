"""Shared fixtures.

Tests run against a real PostgreSQL server. Set REPEATER_TEST_PG to a server URL whose user
can CREATE DATABASE (default: postgresql+psycopg://postgres@127.0.0.1:54329/postgres).
Each test session creates and drops its own database, so parallel runs do not collide.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ADMIN_URL = os.environ.get("REPEATER_TEST_PG", "postgresql+psycopg://postgres@127.0.0.1:54329/postgres")
_DB_NAME = f"repeater_test_{os.getpid()}_{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="session", autouse=True)
def _test_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    from repeater.crypto import generate_key_spec

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{_DB_NAME}"'))
    db_url = make_url(ADMIN_URL).set(database=_DB_NAME).render_as_string(hide_password=False)
    media = tmp_path_factory.mktemp("media")
    env = {
        "REPEATER_ENV": "test",
        "REPEATER_DATABASE_URL": db_url,
        "REPEATER_SECRET_KEY": "test-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz",
        "REPEATER_TOKEN_ENCRYPTION_KEYS": generate_key_spec("t1"),
        "REPEATER_MEDIA_ROOT": str(media),
        "REPEATER_BASE_URL": "https://rotator.test",
        "REPEATER_TIKTOK_CLIENT_KEY": "tt-key",
        "REPEATER_TIKTOK_CLIENT_SECRET": "tt-secret",
        "REPEATER_GOOGLE_CLIENT_ID": "g-id",
        "REPEATER_GOOGLE_CLIENT_SECRET": "g-secret",
        "REPEATER_META_APP_ID": "m-id",
        "REPEATER_META_APP_SECRET": "m-secret",
        # The suite exercises an audited TikTok app (any privacy level); the unaudited path has its
        # own tests that switch the setting off.
        "REPEATER_TIKTOK_AUDITED": "true",
        "REPEATER_TELEGRAM_BOT_TOKEN": "",
        "REPEATER_TELEGRAM_CHAT_ID": "",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    from repeater.config import reset_settings_cache
    from repeater.db import Base, get_engine, reset_engine_cache

    reset_settings_cache()
    reset_engine_cache()
    import repeater.models  # noqa: F401

    Base.metadata.create_all(get_engine())
    yield
    reset_engine_cache()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{_DB_NAME}" WITH (FORCE)'))
    admin.dispose()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    reset_settings_cache()


def _truncate_all() -> None:
    from repeater.db import Base, get_engine

    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_engine().begin() as c:
        c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db() -> Iterator[Session]:
    """A session on the test database. All tables are truncated after each test."""
    from repeater.db import get_sessionmaker

    s = get_sessionmaker()()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        _truncate_all()


@pytest.fixture
def workspace(db: Session):
    from repeater.models import User, Workspace, WorkspaceMember
    from repeater.security import hash_password

    ws = Workspace(name="Magic Lab", timezone="Africa/Cairo")
    user = User(email="admin@magic-lap.com", password_hash=hash_password("correct horse battery"))
    db.add_all([ws, user])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
    db.commit()
    return ws


@pytest.fixture
def admin_user(db: Session, workspace):
    from sqlalchemy import select

    from repeater.models import User

    return db.execute(select(User).where(User.email == "admin@magic-lap.com")).scalar_one()


@pytest.fixture
def make_account(db: Session, workspace):
    """Factory: make_account("tiktok", external_id="x", expires_in=timedelta(hours=24), extra={...})."""
    from repeater.accounts import save_tokens
    from repeater.models import ConnectedAccount, Platform
    from repeater.platforms.base import OAuthTokens
    from repeater.timeutil import utcnow

    counter = {"n": 0}

    def _make(
        platform: str,
        *,
        external_id: str | None = None,
        expires_in: timedelta | None = timedelta(hours=24),
        extra: dict | None = None,
        display_name: str | None = None,
        workspace_id: int | None = None,
        refresh_token: str | None = "refresh-token",
        access_token: str = "access-token",
    ):
        counter["n"] += 1
        acc = ConnectedAccount(
            workspace_id=workspace_id or workspace.id,
            platform=Platform(platform),
            external_id=external_id or f"{platform}-{counter['n']}",
            display_name=display_name or f"{platform} account {counter['n']}",
            extra=extra or {},
        )
        save_tokens(
            acc,
            OAuthTokens(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=(utcnow() + expires_in) if expires_in else None,
                scopes=["test"],
            ),
        )
        db.add(acc)
        db.commit()
        return acc

    return _make


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real 4-second 1080x1920 H.264/AAC MP4 generated with ffmpeg."""
    out = tmp_path_factory.mktemp("samples") / "sample_vertical.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1080x1920:rate=30:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        check=True,
    )
    return out


@pytest.fixture(scope="session")
def sample_video_landscape(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 3-second 1280x720 MP4 at 25 fps (to exercise scaling/padding)."""
    out = tmp_path_factory.mktemp("samples") / "sample_landscape.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=25:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        check=True,
    )
    return out
