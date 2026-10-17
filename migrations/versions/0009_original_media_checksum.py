"""Separate original-file checksums from canonical image deduplication.

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("videos", sa.Column("original_sha256", sa.String(64), nullable=True))
    # Legacy video sha256 hashes the original file. Image sha256 hashes normalized pixels and
    # must never be copied here. Existing image bytes are verified by remote read-back/manifests.
    op.execute("UPDATE videos SET original_sha256=sha256 WHERE media_kind='video'")


def downgrade():
    op.drop_column("videos", "original_sha256")
