"""Upgrade/downgrade the shipped 0001 schema without losing existing accounts."""

from __future__ import annotations

import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from repeater.db import Base
from tests.conftest import ADMIN_URL


def test_access_migration_roundtrip_preserves_existing_accounts():
    name = "repeater_migration_" + uuid.uuid4().hex[:12]
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(make_url(ADMIN_URL).set(database=name))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    try:
        command.upgrade(cfg, "0001")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id,email,password_hash,is_active,locale) VALUES (1,'existing@example.com','unchanged-hash',true,'ar')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO workspaces (id,name,timezone,locale,paused,cross_platform_gap_minutes,horizon_hours) VALUES (1,'Existing','Africa/Cairo','ar',true,1440,48)"
                )
            )
            conn.execute(
                text("INSERT INTO workspace_members (workspace_id,user_id,role) VALUES (1,1,'owner')")
            )
        for _ in range(2):
            command.upgrade(cfg, "0003")
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO creator_profiles (id,workspace_id,name) VALUES (1,1,'Existing person')")
                )
                conn.execute(
                    text(
                        "INSERT INTO content_plans (id,workspace_id,profile_id,timezone,start_date,weekdays,times,ordering,revision) VALUES (1,1,1,'UTC','2027-01-01','[0,1,2,3,4,5,6]','[\"10:00\"]','oldest',3)"
                    )
                )
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO videos (id,workspace_id,title,caption,status,overrides) VALUES (1,1,'Existing video','','ready','{}') ON CONFLICT DO NOTHING"
                    )
                )
            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                assert conn.execute(
                    text("SELECT rotation_mode,repeat_after_days,revision FROM content_plans WHERE id=1")
                ).one() == ("once", 30, 3)
                assert conn.execute(
                    text("SELECT password_hash,is_platform_admin FROM users WHERE id=1")
                ).one() == ("unchanged-hash", False)
                assert (
                    conn.execute(text("SELECT role FROM workspace_members WHERE user_id=1")).scalar_one()
                    == "owner"
                )
                assert conn.execute(
                    text("SELECT media_kind,content_format,status FROM videos WHERE id=1")
                ).one() == ("video", "reel", "ready")
                assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO videos (id,workspace_id,profile_id,title,caption,status,overrides,media_kind,content_format) VALUES (2,1,1,'Rich item','Text body','ready','{}','text','post') ON CONFLICT (id) DO UPDATE SET profile_id=1,media_kind='text',content_format='post',status='ready'"
                    )
                )
            command.downgrade(cfg, "0005")
            with engine.connect() as conn:
                assert conn.execute(text("SELECT status FROM videos WHERE id=2")).scalar_one() == "archived"
                assert conn.execute(text("SELECT status FROM videos WHERE id=1")).scalar_one() == "ready"
            command.downgrade(cfg, "0001")
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
