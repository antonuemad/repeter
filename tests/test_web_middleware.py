"""Request body limits, flash message budget and small web helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace

from repeater.web import templating
from repeater.web.deps import Page, safe_next
from repeater.web.middleware import BodySizeLimitMiddleware
from repeater.web.templating import fmt_bytes, fmt_duration
from tests.test_web_support import csrf_token

pytest_plugins = ["tests.test_web_support"]


def test_large_form_rejected_by_content_length(web_owner):
    token = csrf_token(web_owner, "/")
    resp = web_owner.post(
        "/settings/workspace",
        data={"csrf_token": token, "junk": "x" * (3 * 1024 * 1024)},
        follow_redirects=False,
    )
    assert resp.status_code == 413


def test_large_chunked_body_rejected_while_streaming(web_owner):
    token = csrf_token(web_owner, "/")

    def body():
        yield f"csrf_token={token}&junk=".encode()
        for _ in range(40):
            yield b"x" * 65536

    resp = web_owner.post(
        "/settings/workspace",
        content=body(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code == 413
    assert "الحجم كبير جدًا" in resp.text


async def _call(middleware: BodySizeLimitMiddleware, body: bytes, path: str = "/x") -> int:
    status: list[int] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])

    scope = {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": [(b"content-length", str(len(body)).encode())],
    }
    await middleware(scope, receive, send)
    return status[0]


def test_body_limit_middleware_unit():
    import anyio

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = BodySizeLimitMiddleware(app, max_bytes=10, path_limits={"/upload": 100})
    assert anyio.run(_call, mw, b"small") == 200
    assert anyio.run(_call, mw, b"x" * 11) == 413
    # A streaming path has its own larger cap, never "no cap" (the limit runs before login).
    assert anyio.run(_call, mw, b"x" * 11, "/upload") == 200
    assert anyio.run(_call, mw, b"x" * 101, "/upload") == 413


def test_flash_stays_within_cookie_budget():
    request = SimpleNamespace(session={})
    for _ in range(10):
        templating.flash(request, "error", text="خطأ" * 500)
    items = request.session[templating.FLASH_KEY]
    assert len(items) == 1 and len(items[0]["text"]) == templating.MAX_FLASH_CHARS
    request = SimpleNamespace(session={})
    for i in range(10):
        templating.flash(request, "info", "library.saved", n=i)
    items = request.session[templating.FLASH_KEY]
    assert len(items) == templating.MAX_FLASHES and items[-1]["params"] == {"n": "9"}
    assert len(json.dumps(items)) <= templating.MAX_FLASH_JSON_BYTES


def test_pop_flashes_translates():
    request = SimpleNamespace(session={})
    templating.flash(request, "error", "error.field_required", field="daily_cap")
    templating.flash(request, "error", "oauth.exchange_failed", text="Bad code.", platform="TikTok")
    messages = templating.pop_flashes(request, "en")
    assert [m["message"] for m in messages] == [
        "Posts per day is required.",
        "Could not connect TikTok: Bad code.",
    ]
    assert templating.FLASH_KEY not in request.session


def test_safe_next():
    assert safe_next("/library?status=ready") == "/library?status=ready"
    for bad in (
        None,
        "",
        "https://evil.example",
        "//evil.example",
        "/\\evil",
        "javascript:alert(1)",
        "/ok\r\nX: y",
    ):
        assert safe_next(bad) == "/", bad


def test_page_links_and_formatters():
    page = Page(number=2, size=10, total=25)
    assert (page.offset, page.pages, page.has_prev, page.has_next) == (10, 3, True, True)
    assert page.link("/activity", 3, level="error", category="") == "/activity?level=error&page=3"
    assert fmt_bytes(None) == "—" and fmt_bytes(512) == "512 B" and fmt_bytes(1536) == "1.5 KB"
    assert fmt_duration(65.4) == "1:05" and fmt_duration(None) == "—"
