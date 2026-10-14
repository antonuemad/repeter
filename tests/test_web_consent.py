"""Terms and privacy consent before first use, the legal texts and the language of public pages."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from repeater.models import ActivityLog, DataDeletionRequest, User
from repeater.web import consent, i18n
from tests.test_web_support import EDITOR_EMAIL, PASSWORD, login, post, set_lang

pytest_plugins = ["tests.test_web_support"]

GATED = ("/", "/library", "/schedule", "/accounts", "/rules", "/sources", "/activity")


def _consents(db, user_id: int) -> list[ActivityLog]:
    rows = db.execute(select(ActivityLog).where(ActivityLog.category == "auth")).scalars()
    return [r for r in rows if r.data.get("event") == "terms_accepted" and r.data.get("user_id") == user_id]


# --------------------------------------------------------------------------- consent screen


def test_nothing_works_before_the_terms_are_accepted(web_client, db, workspace):
    assert login(web_client, accept_terms=False).status_code == 303
    for path in GATED:
        resp = web_client.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/consent?next="), path
    resp = web_client.get("/library?status=ready", follow_redirects=False)
    assert resp.headers["location"] == "/consent?next=%2Flibrary%3Fstatus%3Dready"
    # Actions are refused too, and do nothing.
    form = {"timezone": "Europe/Berlin", "cross_gap_hours": "1", "horizon_hours": "48"}
    resp = post(web_client, "/settings/workspace", form, page="/consent")
    assert resp.status_code == 303 and resp.headers["location"].startswith("/consent")
    db.expire_all()
    from repeater.models import Workspace

    assert db.get(Workspace, workspace.id).timezone == "Africa/Cairo"


def test_consent_screen_records_acceptance_with_version_and_time(web_client, db, workspace, admin_user):
    login(web_client, accept_terms=False)
    set_lang(web_client, "en")
    page = web_client.get("/consent?next=/library")
    assert page.status_code == 200
    html = page.text
    assert 'href="/terms"' in html and 'href="/privacy"' in html
    assert 'name="accept" value="1" required' in html and 'name="next" value="/library"' in html
    assert "https://www.youtube.com/t/terms" in html
    assert consent.LEGAL_VERSION in html
    # Without the box ticked nothing is recorded.
    resp = post(web_client, "/consent", {"next": "/library"}, page="/consent")
    assert resp.status_code == 400 and "Tick the box" in resp.text
    assert _consents(db, admin_user.id) == []
    assert web_client.get("/", follow_redirects=False).status_code == 303
    # Accepting continues where the user was going.
    resp = post(web_client, "/consent", {"accept": "1", "next": "/library"}, page="/consent")
    assert resp.status_code == 303 and resp.headers["location"] == "/library"
    assert web_client.get("/").status_code == 200
    (entry,) = _consents(db, admin_user.id)
    assert entry.data["policy_version"] == consent.LEGAL_VERSION
    assert entry.data["accepted_at"] and entry.actor == admin_user.email
    # An open redirect is not possible, and the screen is skipped once accepted.
    assert (
        web_client.get("/consent?next=https://evil.example", follow_redirects=False).headers["location"]
        == "/"
    )
    # Accepting twice records once.
    post(web_client, "/consent", {"accept": "1"}, page="/library")
    assert len(_consents(db, admin_user.id)) == 1


def test_acceptance_is_remembered_across_sessions_until_the_version_changes(
    web_app, web_client, db, workspace, monkeypatch
):
    login(web_client)  # signs in and accepts
    with TestClient(web_app, base_url="https://rotator.test") as second:
        assert login(second, accept_terms=False).status_code == 303
        assert second.get("/", follow_redirects=False).status_code == 200
    monkeypatch.setattr(consent, "LEGAL_VERSION", "2099-01-01")
    with TestClient(web_app, base_url="https://rotator.test") as third:
        login(third, accept_terms=False)
        resp = third.get("/", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/consent")
        accept = post(third, "/consent", {"accept": "1"}, page="/consent")
        assert accept.status_code == 303
        assert third.get("/", follow_redirects=False).status_code == 200


def test_editor_must_accept_too(web_app, db, workspace):
    from repeater.models import WorkspaceMember
    from repeater.security import hash_password

    user = User(email=EDITOR_EMAIL, password_hash=hash_password(PASSWORD))
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="editor"))
    db.commit()
    with TestClient(web_app, base_url="https://rotator.test") as client:
        login(client, EDITOR_EMAIL, accept_terms=False)
        assert client.get("/library", follow_redirects=False).headers["location"].startswith("/consent")


def test_public_pages_and_sign_out_work_without_consent(web_client, workspace):
    login(web_client, accept_terms=False)
    for path in ("/privacy", "/terms", "/healthz", "/robots.txt", "/login?next=/"):
        assert web_client.get(path, follow_redirects=False).status_code in (200, 303), path
    for path in ("/privacy", "/terms"):
        assert web_client.get(path).status_code == 200
    resp = post(web_client, "/logout", page="/consent")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_consent_screen_needs_a_signed_in_user(web_client):
    resp = web_client.get("/consent", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_login_page_names_the_terms_and_privacy_policy(web_client):
    set_lang(web_client, "en")
    html = web_client.get("/login").text
    main = html.split("<main", 1)[1]
    assert (
        'By signing in you agree to our <a href="/terms">Terms of Service</a> and <a href="/privacy">' in main
    )
    set_lang(web_client, "ar")
    main = web_client.get("/login").text.split("<main", 1)[1]
    assert 'href="/terms"' in main and 'href="/privacy"' in main and "توافق على" in main


def test_consent_is_stored_on_the_user_with_version_and_time(db, workspace, admin_user):
    """User.terms_accepted_at and terms_version are the record; the activity entry is an audit
    trail that the 90-day log purge may remove without asking anyone to accept again."""
    session: dict = {}
    assert admin_user.terms_accepted_at is None and admin_user.terms_version is None
    assert consent.has_consented(session, db, admin_user) is False
    consent.record_consent(session, db, admin_user)
    db.commit()
    db.refresh(admin_user)
    assert admin_user.terms_version == consent.LEGAL_VERSION and admin_user.terms_accepted_at is not None
    assert consent.has_consented({}, db, admin_user) is True
    db.execute(ActivityLog.__table__.delete())  # the audit trail is gone: still accepted
    db.commit()
    assert consent.has_consented({}, db, admin_user) is True
    admin_user.terms_version = "1999-01-01"
    assert consent.has_consented({}, db, admin_user) is False


def test_session_cache_is_bound_to_user_and_version(db, workspace, admin_user):
    other = SimpleNamespace(id=admin_user.id + 1, email="x@y.z", terms_accepted_at=None, terms_version=None)
    session = {consent.SESSION_KEY: f"{admin_user.id}:{consent.LEGAL_VERSION}"}
    assert consent.has_consented(session, db, admin_user) is False
    consent.record_consent(session, db, admin_user)
    assert consent.has_consented(session, db, admin_user) is True
    assert consent.has_consented(session, db, other) is False  # type: ignore[arg-type]
    admin_user.terms_version = "1999-01-01"
    session = {consent.SESSION_KEY: f"{admin_user.id}:1999-01-01"}
    assert consent.has_consented(session, db, admin_user) is False


# --------------------------------------------------------------------------- language of public pages


def test_accept_language_parsing():
    parse = i18n.accept_language
    assert parse("en-US,en;q=0.9") == "en"
    assert parse("ar-EG,ar;q=0.9,en;q=0.8") == "ar"
    assert parse("ar;q=0.4, en;q=0.9") == "en"
    assert parse("fr-FR,fr;q=0.9,*;q=0.1") is None
    assert parse("fr, en;q=0.5") == "en"
    assert parse("en;q=0, ar;q=0.1") == "ar"
    assert parse("en;q=oops, ar") == "ar"
    assert parse("") is None and parse(None) is None
    assert parse("x" * 5000) is None


def _page_lang(response) -> str:
    return "en" if '<html lang="en" dir="ltr">' in response.text else "ar"


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_legal_pages_open_in_english_without_a_cookie(web_app, path):
    with TestClient(web_app, base_url="https://rotator.test") as client:
        resp = client.get(f"{path}?lang=en")
        assert resp.status_code == 200 and _page_lang(resp) == "en"
        assert "repeater_lang=en" in resp.headers["set-cookie"]
        assert _page_lang(client.get(path)) == "en"  # remembered
    with TestClient(web_app, base_url="https://rotator.test") as client:
        assert _page_lang(client.get(path)) == "ar"  # default for visitors without a signal
        assert _page_lang(client.get(path, headers={"Accept-Language": "en-US,en;q=0.9"})) == "en"
        assert _page_lang(client.get(path, headers={"Accept-Language": "ar-EG,ar;q=0.9"})) == "ar"
        assert _page_lang(client.get(path, headers={"Accept-Language": "fr-FR,fr;q=0.9"})) == "ar"
        assert _page_lang(client.get(f"{path}?lang=xx", headers={"Accept-Language": "en"})) == "en"


def test_language_precedence(web_app):
    with TestClient(web_app, base_url="https://rotator.test") as client:
        client.cookies.set("repeater_lang", "ar")
        # An explicit choice on this device beats the browser's language, and ?lang= beats both.
        assert _page_lang(client.get("/privacy", headers={"Accept-Language": "en"})) == "ar"
        assert _page_lang(client.get("/privacy?lang=en", headers={"Accept-Language": "ar"})) == "en"


def test_switching_language_from_a_lang_url_works(web_app):
    with TestClient(web_app, base_url="https://rotator.test") as client:
        assert _page_lang(client.get("/privacy?lang=en")) == "en"
        page = client.get("/privacy?lang=en").text
        token = page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
        resp = client.post(
            "/lang",
            data={"lang": "ar", "next": "/privacy?lang=en&x=1", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/privacy?x=1"
        assert _page_lang(client.get(resp.headers["location"])) == "ar"


def test_deletion_status_page_in_english_by_url(web_app, db):
    db.add(DataDeletionRequest(provider="meta", external_user_id="9", confirmation_code="abc123def456"))
    db.commit()
    with TestClient(web_app, base_url="https://rotator.test") as client:
        resp = client.get("/meta/deletion-status/abc123def456?lang=en")
        assert _page_lang(resp) == "en" and "Data deletion request" in resp.text


def test_deletion_status_says_what_is_deleted(web_app, db):
    db.add(DataDeletionRequest(provider="meta", external_user_id="9", confirmation_code="abc123def456"))
    db.commit()
    with TestClient(web_app, base_url="https://rotator.test") as client:
        html = client.get("/meta/deletion-status/abc123def456?lang=en").text
        for needle in (
            "Within 24 hours",
            "the deletion starts immediately",
            "Facebook Pages and Instagram accounts",
            "the videos imported from them and their files",
            "the posts and publishing history of those accounts",
            "every activity-log entry that names them",
        ):
            assert needle in html, needle
        html = client.get("/meta/deletion-status/abc123def456?lang=ar").text
        assert "خلال 24 ساعة" in html and "سجل النشاط" in html and "ملفاتها" in html


# --------------------------------------------------------------------------- the texts


def _text(client: TestClient, path: str, lang: str) -> str:
    return client.get(f"{path}?lang={lang}").text.replace("&#39;", "'")


def test_privacy_policy_english_matches_the_retention_rules(web_app):
    with TestClient(web_app, base_url="https://rotator.test") as client:
        html = _text(client, "/privacy", "en")
    # Limited Use: Google's wording, with explicit prior consent for a merger (ux-compliance-5).
    assert "explicit prior consent" in html
    assert "with your notice" not in html
    for needle in (
        "with your consent",
        "for security purposes",
        "to comply with applicable law",
        "merger, acquisition or sale of assets",
    ):
        assert needle in html, needle
    # Alerts to the operators through Telegram (ux-compliance-10).
    assert "Telegram" in html and "https://telegram.org/privacy" in html
    assert "title of the video" in html and "name of the connected account" in html
    # C5 (a)-(e).
    assert "deleted immediately when you disconnect the account" in html
    assert "within 7 days of disconnecting an account we delete the account record" in html
    assert "post history (platform post IDs and links)" in html
    assert "activity-log entries that name it" in html
    assert "Videos imported from that account stay in your library until you delete them" in html
    assert "refreshed or removed within 30 days" in html
    assert "completed within 24 hours" in html
    # Backups keep deleted data for a bounded time (backup.sh: 14 days local, 30 off-site and for
    # the safety dumps).
    assert "Backups:</strong>" in html and "until they expire (within 30 days at the latest)" in html
    assert "Original files</strong> are deleted after 30 days" in html
    assert "failed to process are deleted after 7 days" in html
    # Deletion requests (ux-compliance-missed-4): 7 days, and a Delete button exists.
    assert "We complete deletion requests within 7 days" in html
    assert "We reply within 30 days" not in html
    assert "owners can delete any video" in html
    # TikTok: the grant is kept while another workspace still uses it (api-tiktok-youtube-missed-2).
    assert "unless the same TikTok user is still connected in another Repeater workspace" in html
    # The yt-dlp path never uses connected accounts (C6).
    assert "never uses the tokens or the official API of any of your connected accounts" in html
    # Consent record.
    assert "the time and the version" in html


def test_privacy_policy_arabic_matches_the_retention_rules(web_app):
    with TestClient(web_app, base_url="https://rotator.test") as client:
        html = _text(client, "/privacy", "ar")
    assert "موافقتك الصريحة المسبقة" in html
    assert "إخطارك" not in html
    assert "تيليجرام" in html and "https://telegram.org/privacy" in html
    for needle in (
        "تُحذف فورًا عند فصل الحساب",
        "خلال 7 أيام من فصل الحساب نحذف سجل الحساب",
        "معرّفات المنشورات وروابطها",
        "إدخالات سجل النشاط التي تذكره",
        "حتى تحذفها أو تطلب منا حذف بياناتك",
        "خلال 30 يومًا",
        "تكتمل خلال 24 ساعة",
        "النسخ الاحتياطية:",
        "خلال 30 يومًا على الأكثر",
        "بعد 7 أيام",
        "ننفّذ طلبات الحذف خلال 7 أيام",
        "مستخدم تيك توك نفسه ما زال مربوطًا في مساحة عمل أخرى",
        "ولا واجهتها الرسمية",
    ):
        assert needle in html, needle
    assert "نرد خلال 30 يومًا" not in html.replace("نرد على الطلبات الأخرى خلال 30 يومًا", "")


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_terms_say_that_publishing_starts_off_and_consent_comes_first(web_app, lang):
    with TestClient(web_app, base_url="https://rotator.test") as client:
        html = _text(client, "/terms", lang)
    if lang == "en":
        assert "New accounts start with publishing turned off" in html
        assert "we ask you to accept these terms" in html
    else:
        assert "الحسابات الجديدة والنشر عليها متوقف" in html
        assert "نطلب منك الموافقة على هذه الشروط" in html
