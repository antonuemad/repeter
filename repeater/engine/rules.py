"""Rotation rule defaults. OWNER: engine module."""

from __future__ import annotations

from sqlalchemy.orm import Session

from repeater.models import ConnectedAccount, RotationRule


def ensure_rule(session: Session, account: ConnectedAccount) -> RotationRule | None:
    """Create the default RotationRule for a newly connected publish account if it has none.
    Returns None for non-publish platforms (gdrive). TikTok rules start DISABLED until the user
    picks a privacy level and gives music-usage consent (Content Sharing Guidelines)."""
    raise NotImplementedError
