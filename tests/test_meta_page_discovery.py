"""Facebook Login schema regressions; synthetic OAuth and denied unmocked HTTP only."""

from urllib.parse import parse_qs, urlparse

import pytest
import respx
from sqlalchemy import select

from repeater.accounts import upsert_connected_accounts
from repeater.config import get_settings
from repeater.media.content import problem
from repeater.models import AccountStatus, ConnectedAccount, Job, Platform, RotationRule, ScheduledPost, Video
from repeater.platforms import meta
from tests.test_platform_meta import (
    CODE,
    IG_A,
    REDIRECT,
    accounts_endpoint,
    assert_signed,
    graph_error_response,
    mock_login,
    query,
)
from tests.test_web_support import set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.fixture(autouse=True)
def locked_fake_graph(monkeypatch):
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    with respx.mock(assert_all_called=False) as graph:
        yield graph
        assert all(call.request.method == "GET" for call in graph.calls)
        assert not get_settings().publishing_enabled


def supported_accounts_endpoint(request):
    # Reproduce the actual Graph #100 failure instead of accepting whatever PAGE_FIELDS says.
    fields = query(request).get("fields", "")
    if "account_type" in fields:
        return graph_error_response(400, 100, message="Tried accessing nonexisting field (account_type)")
    assert "instagram_business_account{id,username,name,profile_picture_url}" in fields
    return accounts_endpoint(request)


@pytest.mark.parametrize("purpose", ["meta", "facebook", "instagram"])
def test_facebook_login_discovers_pages_without_instagram_login_only_fields(locked_fake_graph, purpose):
    mock_login(locked_fake_graph, accounts=supported_accounts_endpoint)
    accounts = meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose=purpose)
    expected = {
        "meta": [(Platform.FACEBOOK, "111"), (Platform.INSTAGRAM, IG_A["id"])],
        "facebook": [(Platform.FACEBOOK, "111")],
        "instagram": [(Platform.INSTAGRAM, IG_A["id"])],
    }
    assert [(a.platform, a.external_id) for a in accounts] == expected[purpose]
    page_calls = [c for c in locked_fake_graph.calls if c.request.url.path.endswith("/me/accounts")]
    assert len(page_calls) == 2  # The fix must also apply to the cursor continuation.
    assert assert_signed(locked_fake_graph) == 4
    for account in accounts:
        if account.platform == Platform.INSTAGRAM:
            assert account.extra.get("account_type") is None


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_meta_callback_saves_discovered_accounts_with_publishing_still_locked(
    web_owner, db, locked_fake_graph, lang
):
    mock_login(locked_fake_graph, accounts=supported_accounts_endpoint)
    set_lang(web_owner, lang)
    start = web_owner.get("/connect/meta", follow_redirects=False)
    assert start.status_code == 303
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    response = web_owner.get(
        "/oauth/meta/callback", params={"code": CODE, "state": state}, follow_redirects=False
    )
    assert response.status_code == 303
    accounts = list(db.scalars(select(ConnectedAccount).order_by(ConnectedAccount.platform)))
    assert {a.platform for a in accounts} == {Platform.FACEBOOK, Platform.INSTAGRAM}
    assert all(a.status == AccountStatus.ACTIVE and a.token_enc for a in accounts)
    html = web_owner.get("/accounts").text
    assert "Magic Lap Page" in html and "magiclap" in html
    assert "nonexisting field" not in html
    assert all(not rule.enabled for rule in db.scalars(select(RotationRule)))
    assert not list(db.scalars(select(ScheduledPost)))
    assert set(db.scalars(select(Job.kind))) <= {"plan_workspace"}


def test_meta_reconnect_clears_stale_business_type_without_guessing_story_eligibility(
    db, workspace, make_account, locked_fake_graph
):
    stored = make_account(
        "instagram",
        external_id=IG_A["id"],
        extra={"oauth_provider": "meta", "account_type": "BUSINESS", "page_id": "111"},
    )
    mock_login(locked_fake_graph, accounts=supported_accounts_endpoint)
    items = meta.oauth.exchange_code(code=CODE, redirect_uri=REDIRECT, code_verifier=None, purpose="meta")
    connected = upsert_connected_accounts(db, workspace.id, items)
    db.flush()
    instagram = next(a for a in connected if a.platform == Platform.INSTAGRAM)
    assert instagram.id == stored.id and instagram.status == AccountStatus.ACTIVE
    assert instagram.extra.get("account_type") is None
    story = Video(
        media_kind="image", content_format="story", encoded_path="synthetic.jpg", width=1080, height=1920
    )
    assert problem(story, instagram) == "business_required"
    assert not list(db.scalars(select(ScheduledPost)))
    assert not list(db.scalars(select(Job)))
