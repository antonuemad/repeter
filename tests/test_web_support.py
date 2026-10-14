"""Shared fixtures and helpers for the web dashboard tests (loaded with
`pytest_plugins = ["tests.test_web_support"]`). Fixture names start with `web_` so they never
collide with other modules' fixtures."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

PASSWORD = "correct horse battery"
OWNER_EMAIL = "admin@magic-lap.com"
EDITOR_EMAIL = "editor@magic-lap.com"
_CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    match = _CSRF_RE.search(html) or _FIELD_RE.search(html)
    assert match, "no CSRF token in page"
    return match.group(1)


def csrf_token(client: TestClient, path: str = "/login") -> str:
    resp = client.get(path)
    assert resp.status_code == 200, resp.text[:500]
    return csrf_from(resp.text)


def accept_consent(client: TestClient) -> None:
    """Accept the one-time terms and privacy consent screen (a no-op when already accepted)."""
    page = client.get("/consent", follow_redirects=False)
    if page.status_code != 200:
        return
    resp = client.post(
        "/consent",
        data={"csrf_token": csrf_from(page.text), "accept": "1", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:500]


def login(
    client: TestClient,
    email: str = OWNER_EMAIL,
    password: str = PASSWORD,
    next_url: str = "/",
    *,
    accept_terms: bool = True,
):
    """Sign in. By default the one-time consent screen is accepted right after (the response of
    the sign-in itself is returned); pass accept_terms=False to test the consent screen."""
    token = csrf_token(client, "/login")
    resp = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token, "next": next_url},
        follow_redirects=False,
    )
    if accept_terms and resp.status_code == 303:
        accept_consent(client)
    return resp


def post(client: TestClient, path: str, data: dict[str, Any] | None = None, *, page: str = "/", **kw: Any):
    """POST a form with the session's CSRF token (read from `page`)."""
    payload = {"csrf_token": csrf_token(client, page), **(data or {})}
    return client.post(path, data=payload, follow_redirects=False, **kw)


def set_lang(client: TestClient, lang: str) -> None:
    client.cookies.set("repeater_lang", lang)


@pytest.fixture
def web_plan_calls(monkeypatch) -> Recorder:
    """Records every call of engine.actions.invalidate_plan (contract C2) made by the dashboard and
    calls through to the real function."""
    from repeater.engine import actions

    recorder = Recorder(result=actions.invalidate_plan)
    monkeypatch.setattr(actions, "invalidate_plan", recorder)
    return recorder


@pytest.fixture
def web_app(web_plan_calls):
    from repeater.web.app import create_app

    return create_app()


@pytest.fixture
def web_client(web_app, db: Session, workspace) -> Iterator[TestClient]:
    """An anonymous client (the owner user and workspace exist)."""
    with TestClient(web_app, base_url="https://rotator.test") as client:
        yield client


@pytest.fixture
def web_owner(web_client: TestClient) -> TestClient:
    resp = login(web_client)
    assert resp.status_code == 303, resp.text[:500]
    return web_client


@pytest.fixture
def web_editor(web_app, db: Session, workspace) -> Iterator[TestClient]:
    from repeater.models import User, WorkspaceMember
    from repeater.security import hash_password

    user = User(email=EDITOR_EMAIL, password_hash=hash_password(PASSWORD))
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="editor"))
    db.commit()
    with TestClient(web_app, base_url="https://rotator.test") as client:
        assert login(client, EDITOR_EMAIL).status_code == 303
        yield client


@pytest.fixture
def web_make_video(db: Session, workspace, sample_video: Path):
    """Factory: a Video row with real encoded/thumbnail files under the media root."""
    from repeater.media import storage
    from repeater.models import Video, VideoStatus

    def _make(
        *,
        status: VideoStatus = VideoStatus.READY,
        title: str = "Card trick",
        with_files: bool = True,
        workspace_id: int | None = None,
        **fields: Any,
    ) -> Video:
        video = Video(
            workspace_id=workspace_id or workspace.id,
            status=status,
            title=title,
            caption=fields.pop("caption", "A great trick #magic"),
            duration_s=4.0,
            width=1080,
            height=1920,
            overrides=fields.pop("overrides", {}),
            **fields,
        )
        db.add(video)
        db.flush()
        if with_files:
            folder = storage.ensure_video_dir(video.workspace_id, video.id)
            shutil.copyfile(sample_video, folder / storage.ENCODED_NAME)
            (folder / storage.THUMB_NAME).write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
            video.encoded_path = storage.rel_path(folder / storage.ENCODED_NAME)
            video.thumbnail_path = storage.rel_path(folder / storage.THUMB_NAME)
            video.encoded_size_bytes = (folder / storage.ENCODED_NAME).stat().st_size
        db.commit()
        return video

    return _make


@pytest.fixture
def web_make_rule(db: Session):
    """Factory: the account's rule with COMPLETE platform settings (TikTok privacy level and music
    consent, YouTube audience answer). New rules are created disabled and incomplete."""
    from repeater.engine import rules
    from repeater.models import Platform

    complete = {
        Platform.TIKTOK: {"privacy_level": "PUBLIC_TO_EVERYONE", "music_consent_at": "2026-10-01T10:00:00"},
        Platform.YOUTUBE: {"self_declared_made_for_kids": False},
    }

    def _make(account, *, enabled: bool = False, **settings: Any):
        rule = rules.ensure_rule(db, account)
        assert rule is not None
        rule.settings = {
            **rules.default_settings(account.platform),
            **complete.get(rule.platform, {}),
            **settings,
        }
        rule.enabled = enabled
        db.commit()
        return rule

    return _make


class Recorder:
    """Records calls made to a monkeypatched function."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = result
        self.error = error

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result(*args, **kwargs) if callable(self.result) else self.result
