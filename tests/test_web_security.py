"""Security of the dashboard's unauthenticated surface: request bodies before login, the login
rate limiter and password hashing under load, and the signed media endpoint."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repeater.web import auth
from repeater.web.auth import LoginRateLimiter
from tests.test_web_support import EDITOR_EMAIL, OWNER_EMAIL, PASSWORD, csrf_token

pytest_plugins = ["tests.test_web_support"]

MIB = 1024 * 1024


# --------------------------------------------------------------------------- security-1: no unbounded body


def _asgi_post(
    app, path: str, *, content_type: str, chunks: list[bytes], cookie: str | None = None
) -> tuple[int, int]:
    """POST a streamed body straight to the ASGI app. Returns (status, number of receive() calls):
    a refused request must not have been read."""
    receive_calls = 0
    queue = list(chunks)
    status: list[int] = []

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    headers = [
        (b"host", b"rotator.test"),
        (b"content-type", content_type.encode()),
        (b"content-length", str(sum(len(c) for c in chunks)).encode()),
    ]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("203.0.113.9", 4242),
        "server": ("rotator.test", 443),
    }
    asyncio.run(app(scope, receive, send))
    return status[0], receive_calls


URLENCODED = "application/x-www-form-urlencoded"
THREE_MIB_OF_FIELDS = [b"f%d=" % i + b"x" * MIB + b"&" for i in range(3)]


def test_urlencoded_body_to_upload_is_refused_without_reading_it(web_app, web_client):
    """Anonymous: the CSRF dependency used to buffer the whole url-encoded body (up to ~1 GiB)
    before any authentication, which OOM-killed the container."""
    status, receive_calls = _asgi_post(
        web_app, "/library/upload", content_type=URLENCODED, chunks=THREE_MIB_OF_FIELDS
    )
    assert status == 403
    assert receive_calls == 0


def test_urlencoded_body_to_upload_is_refused_for_a_signed_in_user_without_token(web_app, web_owner):
    cookie = "; ".join(f"{k}={v}" for k, v in web_owner.cookies.items())
    status, receive_calls = _asgi_post(
        web_app, "/library/upload", content_type=URLENCODED, chunks=THREE_MIB_OF_FIELDS, cookie=cookie
    )
    assert status == 403 and receive_calls == 0


def test_other_form_posts_still_stop_at_the_two_mib_cap(web_app, web_client):
    status, _ = _asgi_post(web_app, "/login", content_type=URLENCODED, chunks=THREE_MIB_OF_FIELDS)
    assert status == 413


def test_upload_path_has_a_finite_cap(web_app):
    """The streaming upload route is not "unlimited": a declared body beyond the file limit plus
    multipart overhead is answered with 413 before login, without being read."""
    from repeater.web.routes import library

    too_big = library.upload_limit_bytes() + 3 * library.MULTIPART_OVERHEAD
    receive_calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.disconnect"}

    status: list[int] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/library/upload",
        "raw_path": b"/library/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"rotator.test"),
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", str(too_big).encode()),
        ],
        "client": ("203.0.113.9", 4242),
        "server": ("rotator.test", 443),
    }
    asyncio.run(web_app(scope, receive, send))
    assert status == [413] and receive_calls == 0


def test_multipart_upload_without_a_session_is_not_read(web_client):
    resp = web_client.post(
        "/library/upload", files={"file": ("a.mp4", b"x" * 5000, "video/mp4")}, follow_redirects=False
    )
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


# --------------------------------------------------------------------------- security-3: limiter


def test_try_begin_counts_attempts_atomically():
    limiter = LoginRateLimiter()
    barrier = threading.Barrier(50)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        allowed = limiter.try_begin("ip:1.2.3.4", "email:a@b.c")
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == limiter.max_failures == 10
    assert results.count(False) == 40


def test_try_begin_counts_nothing_when_blocked_and_refund_gives_back_an_attempt():
    now = [0.0]
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, clock=lambda: now[0])
    assert limiter.try_begin("ip:a", "email:x") and limiter.try_begin("ip:a", "email:y")
    # ip:a is at its limit: a request for a fresh email is refused and does not count on email:z.
    assert limiter.try_begin("ip:a", "email:z") is False
    assert limiter.try_begin("ip:b", "email:z") and limiter.try_begin("ip:b", "email:z")
    assert limiter.try_begin("ip:c", "email:z") is False  # email:z reached its limit through ip:b
    limiter.refund("ip:a")
    assert limiter.try_begin("ip:a", "email:w") is True
    now[0] = 61
    assert limiter.try_begin("ip:a", "email:x") is True  # the window moved on


def _slow_verify(state: dict[str, Any], seconds: float = 0.15) -> Callable[[str, str], bool]:
    lock = state.setdefault("lock", threading.Lock())

    def verify(password_hash: str, password: str) -> bool:
        with lock:
            state["running"] = state.get("running", 0) + 1
            state["peak"] = max(state.get("peak", 0), state["running"])
            state["calls"] = state.get("calls", 0) + 1
        time.sleep(seconds)
        with lock:
            state["running"] -= 1
        return False

    return verify


def _login_burst(web_app, email: str, password: str, count: int) -> list[int]:
    statuses: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(count)

    def run() -> None:
        with TestClient(web_app, base_url="https://rotator.test") as client:
            token = csrf_token(client, "/login")
            barrier.wait()
            resp = client.post(
                "/login",
                data={"email": email, "password": password, "csrf_token": token},
                follow_redirects=False,
            )
        with lock:
            statuses.append(resp.status_code)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return statuses


def test_login_burst_is_capped_and_hashing_is_bounded(web_app, workspace, monkeypatch):
    """30 concurrent wrong-password logins from one address: at most the limiter's budget is ever
    evaluated (it used to be ~4-5x that), and never more than the hash slots run at once."""
    state: dict[str, Any] = {}
    monkeypatch.setattr(auth, "verify_password", _slow_verify(state))
    monkeypatch.setattr(auth, "_hash_slots", threading.BoundedSemaphore(2))
    monkeypatch.setattr(auth, "HASH_WAIT_SECONDS", 10.0)
    statuses = _login_burst(web_app, OWNER_EMAIL, "wrong password", 30)
    assert state["calls"] == 10
    assert state["peak"] <= 2
    assert statuses.count(400) == 10 and statuses.count(429) == 20


def test_empty_password_never_reaches_the_hasher(web_app, workspace, monkeypatch):
    """An empty email or password used to cost a 64 MiB argon2 run per request with no database
    connection to bound it, enough to OOM-kill the container."""
    state: dict[str, Any] = {}
    monkeypatch.setattr(auth, "verify_password", _slow_verify(state, 0.01))
    statuses = _login_burst(web_app, "u@example.com", "", 20)
    assert statuses == [400] * 20
    assert state.get("calls", 0) == 0
    assert _login_burst(web_app, "", "password", 3) == [400] * 3
    assert _login_burst(web_app, OWNER_EMAIL, "x" * (auth.MAX_PASSWORD_CHARS + 1), 3) == [400] * 3
    assert state.get("calls", 0) == 0


def test_busy_hashing_answers_503_and_does_not_burn_the_attempt_budget(web_app, workspace, monkeypatch):
    state: dict[str, Any] = {}
    monkeypatch.setattr(auth, "verify_password", _slow_verify(state, 0.01))
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(auth, "_hash_slots", slots)
    monkeypatch.setattr(auth, "HASH_WAIT_SECONDS", 0.05)
    slots.acquire()
    try:
        statuses = [_login_burst(web_app, OWNER_EMAIL, "wrong", 1)[0] for _ in range(14)]
    finally:
        slots.release()
    assert statuses == [503] * 14 and state.get("calls", 0) == 0
    # Nothing was guessed, so nothing was counted: the next real attempt is evaluated.
    assert _login_burst(web_app, OWNER_EMAIL, "wrong", 1) == [400]
    assert state["calls"] == 1


def test_correct_login_still_works_and_does_not_count_against_the_address(web_app, workspace):
    for _ in range(12):  # more than the 10 failures allowed per window
        with TestClient(web_app, base_url="https://rotator.test") as client:
            token = csrf_token(client, "/login")
            resp = client.post(
                "/login",
                data={"email": OWNER_EMAIL, "password": PASSWORD, "csrf_token": token},
                follow_redirects=False,
            )
            assert resp.status_code == 303


def test_unknown_inactive_and_wrong_users_all_cost_one_verification(
    web_app, db, workspace, admin_user, monkeypatch
):
    state: dict[str, Any] = {}
    monkeypatch.setattr(auth, "verify_password", _slow_verify(state, 0.0))
    admin_user.is_active = False
    db.commit()
    assert _login_burst(web_app, OWNER_EMAIL, PASSWORD, 1) == [400]
    assert _login_burst(web_app, "nobody@example.com", PASSWORD, 1) == [400]
    assert state["calls"] == 2


def test_login_releases_the_database_connection_before_hashing(web_app, workspace, monkeypatch):
    """The argon2 run must not hold a pooled connection (it starved the pool under a flood)."""
    from repeater.db import get_engine

    seen: dict[str, int] = {}

    def verify(password_hash: str, password: str) -> bool:
        seen["checked_out"] = get_engine().pool.checkedout()
        return False

    monkeypatch.setattr(auth, "verify_password", verify)
    _login_burst(web_app, OWNER_EMAIL, "wrong", 1)
    assert seen["checked_out"] == 0


# --------------------------------------------------------------------------- security-4: media signature


@pytest.mark.parametrize(
    "path",
    [
        "/m/1/9999999999/%C3%A9.mp4",  # non-ASCII signature: used to raise TypeError -> 500
        "/m/1/9999999999/%D8%B3%D9%8A%D8%AC.mp4",
        "/m/%C2%B2/9999999999/abc.mp4",  # a superscript "digit" is not a number
        "/m/1/%D9%A3%D9%A3/abc.mp4",  # Arabic-Indic digits are not ASCII digits
        "/m/1/9999999999/abc.mp4",
        "/m/1/9999999999/" + "a" * 500 + ".mp4",
    ],
)
def test_media_endpoint_answers_403_for_malformed_links(web_client, path):
    for method in (web_client.get, web_client.head):
        resp = method(path)
        assert resp.status_code == 403, path


def test_editor_user_exists_fixture_sanity(web_editor):
    # Keeps the editor fixture honest for the other tests of this module family.
    assert web_editor.get("/").status_code == 200
    assert EDITOR_EMAIL in web_editor.get("/").text


def _cookie_header(client: TestClient) -> str:
    jar = SimpleCookie()
    for k, v in client.cookies.items():
        jar[k] = v
    return "; ".join(f"{k}={m.value}" for k, m in jar.items())
