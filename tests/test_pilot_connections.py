"""Invited-pilot journeys use synthetic accounts and intercept every external HTTP request."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from sqlalchemy import select

from repeater.accounts import load_tokens
from repeater.config import get_settings
from repeater.engine import actions, rules, scheduler
from repeater.jobs import kinds
from repeater.media import storage
from repeater.models import (
    ConnectedAccount,
    Job,
    Platform,
    RotationRule,
    ScheduledPost,
    Source,
    Video,
    VideoStatus,
)
from repeater.platforms import google, tiktok
from repeater.sources import drive, importer
from repeater.timeutil import utcnow
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]

FOLDER_ID = "pilotFolder000000000000001"


def _start(client, purpose):
    response = client.get(f"/connect/{purpose}", follow_redirects=False)
    assert response.status_code == 303
    return parse_qs(urlparse(response.headers["location"]).query)


def _finish(client, provider, state):
    response = client.get(
        f"/oauth/{provider}/callback",
        params={"state": state, "code": "synthetic-code"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"]


def test_drive_and_tiktok_connect_import_prepare_without_any_publication(
    web_owner, db, workspace, make_account, sample_video, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "publishing_enabled", False)
    monkeypatch.setattr(settings, "tiktok_audited", False)
    monkeypatch.setattr(settings, "encode_preset", "ultrafast")
    set_lang(web_owner, "en")
    youtube = make_account("youtube")
    youtube_rule = rules.ensure_rule(db, youtube)
    youtube_rule.enabled = True  # the server lock must hold even when a customer enables a rule
    youtube_rule.settings = {**youtube_rule.settings, "self_declared_made_for_kids": False}
    db.commit()

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.post(google.TOKEN_URL).respond(
            200,
            json={
                "access_token": "synthetic-drive-access",
                "refresh_token": "synthetic-drive-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": " ".join(google.PURPOSE_SCOPES["gdrive"]),
            },
        )
        mock.get(google.USERINFO_URL).respond(
            200, json={"sub": "pilot-drive-user", "email": "pilot@example.test"}
        )
        params = _start(web_owner, "gdrive")
        assert set(params["scope"][0].split()) == set(google.PURPOSE_SCOPES["gdrive"])
        assert params["code_challenge_method"] == ["S256"]
        assert _finish(web_owner, "google", params["state"][0]) == "/accounts"
        account = db.scalar(select(ConnectedAccount).where(ConnectedAccount.platform == Platform.GDRIVE))
        assert account and load_tokens(account).access_token == "synthetic-drive-access"
        assert "synthetic-drive-access" not in account.token_enc
        assert db.scalar(select(RotationRule).where(RotationRule.connected_account_id == account.id)) is None

        mock.post(tiktok.TOKEN_URL).respond(
            200,
            json={
                "access_token": "synthetic-tiktok-access",
                "refresh_token": "synthetic-tiktok-refresh",
                "expires_in": 86400,
                "refresh_expires_in": 31536000,
                "open_id": "pilot-tiktok-user",
                "scope": ",".join(tiktok.SCOPES),
                "token_type": "Bearer",
            },
        )
        mock.get(tiktok.USER_INFO_URL).respond(
            200,
            json={
                "data": {"user": {"open_id": "pilot-tiktok-user", "display_name": "Pilot creator"}},
                "error": {"code": "ok"},
            },
        )
        mock.post(tiktok.CREATOR_INFO_URL).respond(
            200,
            json={
                "data": {
                    "creator_username": "pilot_creator",
                    "creator_nickname": "Pilot creator",
                    "privacy_level_options": ["SELF_ONLY"],
                    "max_video_post_duration_sec": 300,
                },
                "error": {"code": "ok"},
            },
        )
        params = _start(web_owner, "tiktok")
        destination = _finish(web_owner, "tiktok", params["state"][0])
        creator = db.scalar(select(ConnectedAccount).where(ConnectedAccount.platform == Platform.TIKTOK))
        assert creator and load_tokens(creator).access_token == "synthetic-tiktok-access"
        assert destination == f"/rules/{creator.id}"
        creator_rule = db.scalar(select(RotationRule).where(RotationRule.connected_account_id == creator.id))
        assert creator_rule and not creator_rule.enabled

        response = post(
            web_owner,
            "/sources/new?kind=drive",
            {
                "kind": "drive",
                "name": "Pilot folder",
                "account_id": str(account.id),
                "folder": f"https://drive.google.com/drive/folders/{FOLDER_ID}",
                "poll_interval_minutes": "10",
                "backfill_count": "1",
                "enabled": "1",
            },
            page="/sources/new?kind=drive",
        )
        assert response.status_code == 303
        source = db.scalar(select(Source))
        assert source.config["folder_id"] == FOLDER_ID
        mock.get(f"{drive.API_BASE}/files").respond(
            200,
            json={
                "files": [
                    {
                        "id": "pilot-video",
                        "name": "Synthetic clip.mp4",
                        "mimeType": "video/mp4",
                        "createdTime": (utcnow() - timedelta(hours=1)).isoformat(),
                        "size": str(sample_video.stat().st_size),
                    }
                ]
            },
        )
        mock.get(f"{drive.API_BASE}/files/pilot-video").respond(
            200, content=sample_video.read_bytes(), headers={"content-type": "video/mp4"}
        )
        assert importer.poll_source(db, source.id).created == 1
        video = db.scalar(select(Video))
        job = db.scalar(select(Job).where(Job.kind == kinds.IMPORT_VIDEO))
        importer.import_video(db, video.id, job.payload)
        importer.process_video(db, video.id)
        db.refresh(video)
        assert video.status == VideoStatus.READY and storage.abs_path(video.encoded_path).is_file()
        assert scheduler.plan_workspace(db, workspace.id) == []
        for target in (youtube, creator):
            with pytest.raises(ValueError, match="disabled.*testing"):
                actions.schedule_video_now(db, video, target, actor="owner")
        assert not list(db.scalars(select(ScheduledPost)))
        assert not list(db.scalars(select(Job).where(Job.kind.in_([kinds.PUBLISH_POST, kinds.POLL_POST]))))
        # Unregistered external requests, including all publication endpoints, fail this journey.
        assert all(
            "/post/publish/" not in str(call.request.url) or str(call.request.url) == tiktok.CREATOR_INFO_URL
            for call in mock.calls
        )


@pytest.mark.parametrize("purpose,provider", [("gdrive", "google"), ("tiktok", "tiktok")])
def test_pilot_oauth_refusal_creates_no_accounts_or_publication_jobs(
    web_owner, db, monkeypatch, purpose, provider
):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    with respx.mock(assert_all_mocked=True) as mock:
        params = _start(web_owner, purpose)
        response = web_owner.get(
            f"/oauth/{provider}/callback",
            params={
                "state": params["state"][0],
                "error": "access_denied",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert not mock.calls
    assert not list(db.scalars(select(ConnectedAccount)))
    assert not list(db.scalars(select(ScheduledPost)))
    assert not list(db.scalars(select(Job).where(Job.kind.in_([kinds.PUBLISH_POST, kinds.POLL_POST]))))
