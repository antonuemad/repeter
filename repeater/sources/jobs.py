"""Job handlers for library feeding (kinds and payloads in repeater.jobs.kinds)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from repeater.jobs import kinds
from repeater.jobs.registry import handler
from repeater.sources import importer


@handler(kinds.POLL_SOURCE)
def handle_poll_source(session: Session, payload: dict) -> None:
    importer.poll_source(session, int(payload["source_id"]))


@handler(kinds.IMPORT_VIDEO)
def handle_import_video(session: Session, payload: dict) -> None:
    importer.import_video(session, int(payload["video_id"]), payload)


@handler(kinds.PROCESS_VIDEO)
def handle_process_video(session: Session, payload: dict) -> None:
    importer.process_video(session, int(payload["video_id"]), payload)
