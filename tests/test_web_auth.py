"""Web dashboard: sign-in, sessions, rate limiting, CSRF, security headers, health, language."""

from __future__ import annotations

from sqlalchemy import select

from repeater.models import ActivityLog, User
from repeater.web.auth import LoginRateLimiter
from tests.test_web_support import EDITOR_EMAIL, OWNER_EMAIL, PASSWORD, csrf_from, csrf_token, login, post

pytest_plugins = ["tests.test_web_support"]


def test_login_page_defaults_to_arabic_rtl(web_client):
    resp = web_client.get("/login")
    assert resp.status_code == 200
    assert '<html lang="ar" dir="rtl">' in resp.text
    assert "تسجيل الدخول" in resp.text
    assert 'href="/privacy"' in resp.text and 'href="/terms"' in resp.text


def test_login_page_in_english_with_cookie(web_client):
    web_client.cookies.set("repeater_lang", "en")
    resp = web_client.get("/login")
    assert '<html lang="en" dir="ltr">' in resp.text
    assert "Sign in" in resp.text and "Privacy Policy" in resp.text


def test_unauthenticated_pages_redirect_to_login(web_client):
    resp = web_client.get("/library?status=ready", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Flibrary%3Fstatus%3Dready"


def test_login_success_rotates_session_and_records_login(web_client, db):
    anon_token = csrf_token(web_client)
    anon_cookie = web_client.cookies.get("repeater_session")
    resp = login(web_client, next_url="/schedule")
    assert resp.status_code == 303 and resp.headers["location"] == "/schedule"
    assert web_client.cookies.get("repeater_session") != anon_cookie
    page = web_client.get("/schedule")
    assert page.status_code == 200
    # Session fixation protection: the anonymous CSRF token is no longer valid.
    assert csrf_from(page.text) != anon_token
    db.expire_all()
    user = db.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    assert user.last_login_at is not None
    assert db.execute(select(ActivityLog).where(ActivityLog.category == "auth")).scalars().first()


def test_login_rejects_unsafe_next(web_client):
    resp = login(web_client, next_url="//evil.example/steal")
    assert resp.headers["location"] == "/"


def test_login_failure_is_generic(web_client):
    resp = login(web_client, password="wrong password")
    assert resp.status_code == 400
    assert "البريد الإلكتروني أو كلمة المرور غير صحيحة" in resp.text
    resp = login(web_client, email="nobody@magic-lap.com")
    assert resp.status_code == 400
    assert web_client.get("/", follow_redirects=False).status_code == 303


def test_login_is_case_insensitive_on_email(web_client):
    assert login(web_client, email="ADMIN@Magic-Lap.com").status_code == 303


def test_inactive_user_cannot_login(web_client, db, admin_user):
    admin_user.is_active = False
    db.commit()
    assert login(web_client).status_code == 400


def test_rate_limit_per_ip_blocks_even_correct_password(web_client):
    for _ in range(10):
        assert login(web_client, password="nope").status_code == 400
    resp = login(web_client)
    assert resp.status_code == 429
    assert "15" in resp.text


def test_rate_limit_per_email_across_ips(web_app, db, workspace):
    from fastapi.testclient import TestClient

    for i in range(10):
        with TestClient(web_app, client=(f"10.0.0.{i}", 1000)) as client:
            assert login(client, password="nope").status_code == 400
    with TestClient(web_app, client=("10.0.1.1", 1000)) as client:
        assert login(client).status_code == 429
        # A different email from a fresh IP is not affected.
    with TestClient(web_app, client=("10.0.1.2", 1000)) as client:
        assert login(client, email="other@magic-lap.com").status_code == 400


def test_rate_limiter_window_expires():
    now = [1000.0]
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, clock=lambda: now[0])
    limiter.record_failure("ip:a")
    limiter.record_failure("ip:a")
    assert limiter.blocked("ip:a")
    now[0] += 61
    assert not limiter.blocked("ip:a")
    limiter.record_failure("email:x")
    limiter.reset("email:x")
    assert not limiter.blocked("email:x")


def test_rate_limiter_caps_memory():
    limiter = LoginRateLimiter(max_failures=5, window_seconds=60, max_keys=3)
    for i in range(10):
        limiter.record_failure(f"ip:{i}")
    assert len(limiter._failures) <= 3


def test_logout_requires_csrf_and_clears_session(web_owner):
    assert web_owner.post("/logout", follow_redirects=False).status_code == 403
    resp = post(web_owner, "/logout")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert web_owner.get("/", follow_redirects=False).status_code == 303


def test_password_change_ends_existing_sessions(web_owner, db, admin_user):
    from repeater.security import hash_password

    assert web_owner.get("/").status_code == 200
    admin_user.password_hash = hash_password("a brand new password")
    db.commit()
    assert web_owner.get("/", follow_redirects=False).status_code == 303


def test_csrf_rejects_missing_wrong_and_accepts_header(web_owner):
    resp = web_owner.post("/settings/workspace", data={"timezone": "UTC"}, follow_redirects=False)
    assert resp.status_code == 403
    assert "انتهت صلاحية هذا النموذج" in resp.text
    resp = web_owner.post(
        "/settings/workspace", data={"timezone": "UTC", "csrf_token": "x" * 43}, follow_redirects=False
    )
    assert resp.status_code == 403
    token = csrf_token(web_owner, "/")
    resp = web_owner.post(
        "/settings/workspace",
        data={"timezone": "UTC", "cross_gap_hours": "24", "horizon_hours": "48"},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_csrf_rejects_json_body_without_header(web_owner):
    resp = web_owner.post("/sources/1/poll", json={"csrf_token": "anything"}, follow_redirects=False)
    assert resp.status_code == 403


def test_csrf_rejects_files_outside_upload(web_owner):
    token = csrf_token(web_owner, "/")
    resp = web_owner.post(
        "/settings/workspace",
        data={"csrf_token": token},
        files={"file": ("x.txt", b"hello", "text/plain")},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_security_headers_on_every_response(web_client):
    for path in ("/login", "/robots.txt", "/healthz", "/static/app.css", "/does-not-exist"):
        resp = web_client.get(path)
        csp = resp.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
        assert "img-src 'self' data: https:" in csp and "media-src 'self'" in csp and "base-uri 'none'" in csp
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert web_client.get("/login").headers["cache-control"] == "no-store"


def test_session_cookie_flags(web_client):
    resp = web_client.get("/login")
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("repeater_session=")
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()


def test_not_found_page_is_friendly(web_owner):
    resp = web_owner.get("/library/999999")
    assert resp.status_code == 404
    assert "الصفحة غير موجودة" in resp.text
    assert web_owner.get("/library/not-a-number").status_code == 404


def test_healthz_ok_and_db_down(web_client, monkeypatch):
    assert web_client.get("/healthz").json() == {"ok": True}
    from repeater.web.routes import public

    def boom() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(public, "db_ping", boom)
    resp = web_client.get("/healthz")
    assert resp.status_code == 503 and resp.json() == {"ok": False}


def test_language_switch_sets_cookie_and_user_locale(web_owner, db, admin_user):
    resp = post(web_owner, "/lang", {"lang": "en", "next": "/library"})
    assert resp.status_code == 303 and resp.headers["location"] == "/library"
    assert "repeater_lang=en" in resp.headers["set-cookie"]
    db.expire_all()
    assert db.get(User, admin_user.id).locale == "en"
    assert '<html lang="en" dir="ltr">' in web_owner.get("/library").text
    # Unknown languages and external redirects are ignored.
    resp = post(web_owner, "/lang", {"lang": "fr", "next": "https://evil.example"})
    assert resp.headers["location"] == "/"


def test_user_locale_used_without_cookie(web_client, db, admin_user):
    admin_user.locale = "en"
    db.commit()
    resp = login(web_client)
    assert "repeater_lang=en" in resp.headers.get("set-cookie", "")
    web_client.cookies.delete("repeater_lang")
    assert '<html lang="en" dir="ltr">' in web_client.get("/").text


def test_anonymous_language_switch_on_login_page(web_client):
    resp = post(web_client, "/lang", {"lang": "en", "next": "/login"}, page="/login")
    assert resp.status_code == 303
    assert '<html lang="en"' in web_client.get("/login").text


def test_user_without_workspace_gets_friendly_403(web_app, db, workspace):
    from fastapi.testclient import TestClient

    from repeater.security import hash_password

    db.add(User(email="lonely@magic-lap.com", password_hash=hash_password(PASSWORD)))
    db.commit()
    with TestClient(web_app) as client:
        assert login(client, "lonely@magic-lap.com").status_code == 303
        resp = client.get("/")
        assert resp.status_code == 403
        assert "مساحة عمل" in resp.text


def test_editor_can_sign_in(web_editor):
    assert web_editor.get("/").status_code == 200
    assert EDITOR_EMAIL in web_editor.get("/").text
