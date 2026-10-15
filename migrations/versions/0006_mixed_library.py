"""Typed person library items; existing videos keep their reel format.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("videos", sa.Column("media_kind", sa.String(12), nullable=False, server_default="video"))
    op.add_column("videos", sa.Column("content_format", sa.String(12), nullable=False, server_default="reel"))
    op.create_check_constraint("ck_library_media_format", "videos",
        "(media_kind = 'video' AND content_format IN ('reel', 'story')) OR "
        "(media_kind = 'image' AND content_format IN ('post', 'story')) OR "
        "(media_kind = 'text' AND content_format = 'post')")
    op.create_check_constraint("ck_rich_media_person", "videos", "media_kind = 'video' OR profile_id IS NOT NULL")


def downgrade():
    # Prevent an older, video-only worker from treating rich content as a video.
    op.execute("UPDATE videos SET status = 'archived' WHERE media_kind <> 'video' OR content_format <> 'reel'")
    op.drop_constraint("ck_rich_media_person", "videos", type_="check")
    op.drop_constraint("ck_library_media_format", "videos", type_="check")
    op.drop_column("videos", "content_format")
    op.drop_column("videos", "media_kind")
