"""Clients with a previously cached stylesheet must request the new release's assets."""

import hashlib
import re

from repeater.web import templating

pytest_plugins = ["tests.test_web_support"]


def test_html_loads_the_exact_shipped_css_and_js_with_versioned_urls(web_client):
    html = web_client.get("/login").text
    for name in ("app.css", "app.js"):
        match = re.search(rf"/static/{re.escape(name)}\?v=([a-f0-9]{{16}})", html)
        assert match, f"{name} must bypass a cached URL from a previous deployment"
        response = web_client.get(match[0])
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest().startswith(match[1])


def test_new_process_versions_changed_bytes_even_with_same_file_size(monkeypatch, tmp_path):
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    (tmp_path / "app.css").write_text("old")
    (tmp_path / "app.js").write_text("script")
    before = templating._build_env().globals["asset_versions"]
    (tmp_path / "app.css").write_text("new")
    after = templating._build_env().globals["asset_versions"]
    assert before["app.css"] != after["app.css"]
    assert before["app.js"] == after["app.js"]
