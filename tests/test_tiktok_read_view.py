from datetime import timedelta

import pytest
import respx
from sqlalchemy import select

from repeater.accounts import load_tokens, save_tokens
from repeater.config import get_settings
from repeater.models import AccountStatus, Job, ScheduledPost, Workspace
from repeater.platforms import tiktok
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


def reader(db, make_account, **kwargs):
    account = make_account("tiktok", **kwargs)
    tokens = load_tokens(account)
    tokens.scopes = ["user.info.basic", "video.list"]
    save_tokens(account, tokens)
    db.commit()
    return account


def api(mock, account, videos=None):
    profile = mock.get(tiktok.USER_INFO_URL).respond(
        200,
        json={
            "data": {"user": {"open_id": account.external_id, "display_name": "Read-only creator"}},
            "error": {"code": "ok"},
        },
    )
    listing = mock.post(tiktok.VIDEO_LIST_URL).respond(
        200,
        json={
            "data": {"videos": videos or [], "has_more": False},
            "error": {"code": "ok"},
        },
    )
    return profile, listing


def test_read_view_uses_only_identity_and_video_list_while_publishing_locked(
    web_owner, db, make_account, monkeypatch
):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    account = reader(db, make_account)
    set_lang(web_owner, "en")
    with respx.mock(assert_all_mocked=True) as mock:
        api(
            mock,
            account,
            [
                {
                    "id": "v1",
                    "title": "My original",
                    "duration": 6,
                    "share_url": "https://www.tiktok.com/@creator/video/123",
                }
            ],
        )
        page = web_owner.get(f"/accounts/{account.id}/videos")
        assert [(c.request.method, str(c.request.url).split("?")[0]) for c in mock.calls] == [
            ("GET", tiktok.USER_INFO_URL),
            ("POST", tiktok.VIDEO_LIST_URL),
        ]
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Read-only creator" in page.text and "My original" in page.text
    assert 'href="https://www.tiktok.com/@creator/video/123"' in page.text
    assert "not original files" in page.text and "does not publish" in page.text
    assert "access-token" not in page.text and "refresh-token" not in page.text
    assert not list(db.scalars(select(ScheduledPost)))
    assert not list(db.scalars(select(Job)))


@pytest.mark.parametrize(
    "lang, expected", [("en", "No public videos returned"), ("ar", "لم يرجع أي فيديو عام")]
)
def test_empty_result_is_distinct_from_an_error(web_owner, db, make_account, lang, expected):
    account = reader(db, make_account)
    set_lang(web_owner, lang)
    with respx.mock(assert_all_mocked=True) as mock:
        api(mock, account)
        page = web_owner.get(f"/accounts/{account.id}/videos")
    assert page.status_code == 200 and expected in page.text
    assert "tiktok_read." not in page.text


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.ACTIVE])
def test_disconnected_or_missing_read_scopes_make_no_provider_requests(web_owner, db, make_account, status):
    account = make_account("tiktok")
    account.status = status
    db.commit()
    set_lang(web_owner, "en")
    with respx.mock(assert_all_mocked=True) as mock:
        page = web_owner.get(f"/accounts/{account.id}/videos")
        assert not mock.calls
    assert page.status_code == 200 and "Reconnect" in page.text
    assert "No public videos returned" not in page.text


def test_profile_identity_mismatch_stops_listing(web_owner, db, make_account):
    account = reader(db, make_account)
    set_lang(web_owner, "en")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        profile, listing = api(mock, account)
        profile.respond(200, json={"data": {"user": {"open_id": "someone-else"}}, "error": {"code": "ok"}})
        page = web_owner.get(f"/accounts/{account.id}/videos")
        assert not listing.called
    assert "Reconnect" in page.text and "someone-else" not in page.text


@pytest.mark.parametrize(
    "code,status,recovery",
    [
        ("access_token_invalid", 401, "Reconnect"),
        ("rate_limit_exceeded", 429, "Refresh the list shortly"),
        ("server_error", 503, "Refresh the list shortly"),
    ],
)
def test_provider_failures_do_not_look_like_empty_content(
    web_owner, db, make_account, code, status, recovery
):
    account = reader(db, make_account)
    set_lang(web_owner, "en")
    with respx.mock(assert_all_mocked=True) as mock:
        _, listing = api(mock, account)
        listing.respond(status, json={"error": {"code": code, "message": "access-token refresh-token"}})
        page = web_owner.get(f"/accounts/{account.id}/videos")
    assert page.status_code == 200 and recovery in page.text
    assert "No public videos returned" not in page.text
    assert "access-token" not in page.text and "refresh-token" not in page.text


def test_untrusted_video_links_and_titles_are_safe(web_owner, db, make_account):
    account = reader(db, make_account)
    set_lang(web_owner, "en")
    with respx.mock(assert_all_mocked=True) as mock:
        api(
            mock,
            account,
            [
                {"id": "v1", "title": "<script>alert(1)</script>", "share_url": "javascript:alert(1)"},
                {"id": "v2", "title": "Other", "share_url": "https://tiktok.com.evil.example/video/1"},
            ],
        )
        page = web_owner.get(f"/accounts/{account.id}/videos")
    assert "&lt;script&gt;" in page.text and "<script>alert" not in page.text
    assert "javascript:" not in page.text and "evil.example" not in page.text


def test_read_view_enforces_tenant_platform_and_auth_before_any_api_call(
    web_owner, web_client, db, make_account
):
    other = Workspace(name="Other tenant")
    db.add(other)
    db.commit()
    foreign = reader(db, make_account, workspace_id=other.id)
    wrong = make_account("youtube")
    with respx.mock(assert_all_mocked=True) as mock:
        assert web_owner.get(f"/accounts/{foreign.id}/videos").status_code == 404
        assert web_owner.get(f"/accounts/{wrong.id}/videos").status_code == 404
        assert post(web_owner, "/logout", page="/accounts").status_code == 303
        response = web_client.get(f"/accounts/{foreign.id}/videos", follow_redirects=False)
        assert response.status_code in (303, 403)
        assert not mock.calls


def test_editor_can_read_and_list_link_is_tiktok_only(web_editor, db, make_account):
    account = reader(db, make_account)
    youtube = make_account("youtube")
    set_lang(web_editor, "en")
    listing = web_editor.get("/accounts")
    assert f'href="/accounts/{account.id}/videos"' in listing.text
    assert f'href="/accounts/{youtube.id}/videos"' not in listing.text
    with respx.mock(assert_all_mocked=True) as mock:
        api(mock, account)
        assert web_editor.get(f"/accounts/{account.id}/videos").status_code == 200


def test_expiring_token_is_refreshed_before_reads_without_publication(web_owner, db, make_account):
    account = reader(db, make_account, expires_in=timedelta(seconds=-1))
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(tiktok.TOKEN_URL).respond(
            200,
            json={
                "access_token": "renewed-access",
                "refresh_token": "renewed-refresh",
                "expires_in": 86400,
                "scope": "user.info.basic,video.list",
                "open_id": account.external_id,
            },
        )
        api(mock, account)
        assert web_owner.get(f"/accounts/{account.id}/videos").status_code == 200
    db.expire_all()
    assert load_tokens(account).refresh_token == "renewed-refresh"
    assert not list(db.scalars(select(ScheduledPost)))
