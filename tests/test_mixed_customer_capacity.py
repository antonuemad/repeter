"""Bounded end-to-end mixed uploads and real encodes, using isolated PostgreSQL and no providers."""

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import respx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select

from repeater.config import get_settings
from repeater.content_plans import simulate
from repeater.db import get_sessionmaker
from repeater.jobs import kinds
from repeater.jobs.worker import run_once
from repeater.models import (
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    Job,
    JobStatus,
    PlanTarget,
    Platform,
    ScheduledPost,
    User,
    Video,
    VideoStatus,
    Workspace,
    WorkspaceMember,
)
from repeater.security import hash_password
from repeater.sources.importer import process_video
from repeater.timeutil import utcnow
from repeater.web import consent
from tests.test_web_support import PASSWORD, csrf_from, login

pytest_plugins = ["tests.test_web_support"]


def test_mixed_upload_queue_encode_and_preview_capacity(db, web_app, sample_video, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(get_settings(), "tiktok_audited", False)
    records = []
    password_hash = hash_password(PASSWORD)
    for index in range(8):
        ws = Workspace(name=f"Synthetic media tenant {index}", paused=True, timezone="UTC")
        user = User(
            email=f"load-{index}@example.com",
            password_hash=password_hash,
            terms_version=consent.LEGAL_VERSION,
            terms_accepted_at=utcnow(),
        )
        db.add_all([ws, user])
        db.flush()
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        person = CreatorProfile(workspace_id=ws.id, name=f"Synthetic person {index}")
        db.add(person)
        db.flush()
        account = ConnectedAccount(
            workspace_id=ws.id,
            profile_id=person.id,
            platform=Platform.FACEBOOK,
            external_id=f"synthetic-{index}",
            display_name="Synthetic page; no token",
        )
        plan = ContentPlan(
            workspace_id=ws.id,
            profile_id=person.id,
            timezone="UTC",
            start_date=(utcnow() + timedelta(days=1)).date(),
            weekdays=list(range(7)),
            times=["12:00"],
            rotation_mode="repeat",
            repeat_after_days=7,
            enabled=False,
        )
        db.add_all([account, plan])
        db.flush()
        db.add(PlanTarget(workspace_id=ws.id, plan_id=plan.id, account_id=account.id))
        records.append((ws.id, person.id, plan.id, user.email))
    db.commit()
    image = io.BytesIO()
    Image.effect_noise((2000, 2000), 100).convert("RGB").save(image, format="PNG")
    # Padding remains within a valid image upload; this exercises multi-megabyte streaming too.
    image_bytes = image.getvalue() + bytes(8 * 1024**2)
    video_bytes = sample_video.read_bytes()

    def upload(record):
        workspace_id, profile_id, _, email = record
        started = time.monotonic()
        with TestClient(web_app, base_url="https://rotator.test") as client:
            assert login(client, email, PASSWORD).status_code == 303
            ids = []
            for kind, data, filename, placement, mime in (
                ("video", video_bytes, "synthetic.mp4", "reel", "video/mp4"),
                ("image", image_bytes, "synthetic.png", "post", "image/png"),
            ):
                token = csrf_from(client.get("/library").text)
                response = client.post(
                    "/library/upload",
                    data={
                        "csrf_token": token,
                        "profile_id": str(profile_id),
                        "format": f"{kind}_{placement}",
                        "title": "Synthetic capacity media",
                    },
                    files={"file": (filename, data, mime)},
                    follow_redirects=False,
                )
                assert response.status_code == 303, response.text[:500]
                assert response.headers["location"].startswith("/library/")
                ids.append(int(response.headers["location"].rsplit("/", 1)[-1]))
            with get_sessionmaker()() as session:
                assert all(session.get(Video, item_id).workspace_id == workspace_id for item_id in ids)
            return time.monotonic() - started

    def drain(index):
        count = 0
        while run_once(
            get_sessionmaker(),
            f"synthetic-media-{index}",
            handlers={
                kinds.PROCESS_VIDEO: lambda session, payload: process_video(
                    session, int(payload["video_id"]), payload
                )
            },
            job_kinds=[kinds.PROCESS_VIDEO],
        ):
            count += 1
        return count

    started = time.monotonic()
    with respx.mock(assert_all_called=False) as network:
        with ThreadPoolExecutor(max_workers=4) as pool:
            upload_times = sorted(pool.map(upload, records))
        # Production reserves one of its two worker threads for urgent jobs and admits one
        # encode at a time. Exercise the same bounded media queue rather than bypassing it.
        assert drain(0) == 16
        with get_sessionmaker()() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(Video).where(Video.status == VideoStatus.READY)
                )
                == 16
            ), [(v.id, v.media_kind, v.status, v.error) for v in session.scalars(select(Video))]
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.kind == kinds.PROCESS_VIDEO, Job.status != JobStatus.DONE)
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.kind.in_([kinds.PUBLISH_POST, kinds.POLL_POST]))
                )
                == 0
            )
            for ws_id, person_id, plan_id, _ in records:
                preview = simulate(session, session.get(ContentPlan, plan_id))
                selected = [entry.video for row in preview["rows"] for entry in row.entries if entry.video]
                assert selected and all(
                    v.workspace_id == ws_id and v.profile_id == person_id for v in selected
                )
            assert session.scalar(select(func.count()).select_from(ScheduledPost)) == 0
        assert not network.calls
    report = {
        "synthetic_tenants": 8,
        "uploads": 16,
        "upload_concurrency": 4,
        "encode_workers": 1,
        "uploaded_bytes": 8 * (len(image_bytes) + len(video_bytes)),
        "upload_p95_seconds": round(upload_times[-1], 3),
        "total_seconds": round(time.monotonic() - started, 3),
        "ready_items": 16,
        "provider_calls": 0,
        "scheduled_publications": 0,
        "publishing_enabled": False,
        "scope": "bounded mixed media pipeline, not a production SLA",
    }
    (tmp_path / "mixed-capacity.json").write_text(json.dumps(report, indent=2))
    print("MIXED_CAPACITY_REPORT=" + json.dumps(report))
