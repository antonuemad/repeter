"""User-selected appearance keeps navigation, security and both locales working."""

from __future__ import annotations

import pytest

from repeater.config import get_settings
from repeater.web import i18n
from tests.test_web_support import post, set_lang

pytest_plugins = ["tests.test_web_support"]


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_theme_switch_remembers_choice_without_changing_language_or_page(web_owner, lang):
    set_lang(web_owner, lang)
    assert 'data-theme="light"' in web_owner.get("/accounts").text
    response = post(web_owner, "/theme", {"theme": "dark", "next": "/accounts?view=all"})
    assert response.status_code == 303
    assert response.headers["location"] == "/accounts?view=all"
    page = web_owner.get(response.headers["location"])
    assert page.status_code == 200
    assert 'data-theme="dark"' in page.text
    assert f'<html lang="{lang}" dir="{i18n.direction(lang)}">' in page.text
    assert 'aria-current="page"' in page.text
    post(web_owner, "/theme", {"theme": "light", "next": "/accounts"})
    assert 'data-theme="light"' in web_owner.get("/accounts").text


def test_theme_switch_requires_csrf(web_client):
    response = web_client.post("/theme", data={"theme": "dark"}, follow_redirects=False)
    assert response.status_code == 403
    assert "repeater_theme" not in web_client.cookies


@pytest.mark.parametrize("target", ["https://evil.test/phish", "//evil.test", "/\\evil.test"])
def test_theme_switch_cannot_redirect_outside_the_app(web_client, target):
    response = post(web_client, "/theme", {"theme": "dark", "next": target}, page="/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_invalid_theme_does_not_set_a_cookie_or_inject_markup(web_client):
    response = post(web_client, "/theme", {"theme": '" onload=evil', "next": "/login"}, page="/login")
    assert "repeater_theme" not in response.headers.get("set-cookie", "")
    web_client.cookies.set("repeater_theme", "invalid")
    assert 'data-theme="light"' in web_client.get("/login").text


def test_theme_cookie_is_secure_in_production(web_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "env", "production")
    response = post(web_client, "/theme", {"theme": "dark", "next": "/login"}, page="/login")
    cookie = response.headers["set-cookie"].split("repeater_theme=", 1)[1].split(", repeater_session", 1)[0]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
