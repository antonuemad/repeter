"""Signed public media URLs, robots.txt, and the authenticated thumbnail/preview routes."""

from __future__ import annotations

import time

from repeater import crypto
from repeater.media import storage
from repeater.models import Workspace

pytest_plugins = ["tests.test_web_support"]


def _url(video_id: int, ttl: int = 3600) -> str:
    return crypto.signed_media_path(video_id, ttl_seconds=ttl)


def test_signed_media_get_serves_file(web_client, web_make_video, sample_video):
    video = web_make_video()
    resp = web_client.get(_url(video.id))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.headers["cache-control"] == "private, max-age=3600"
    assert "noindex" in resp.headers["x-robots-tag"]
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == sample_video.read_bytes()
    assert "set-cookie" not in resp.headers


def test_signed_media_ignores_gzip(web_client, web_make_video, sample_video):
    video = web_make_video()
    resp = web_client.get(_url(video.id), headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in resp.headers
    assert resp.content == sample_video.read_bytes()


def test_signed_media_head(web_client, web_make_video, sample_video):
    video = web_make_video()
    resp = web_client.head(_url(video.id))
    assert resp.status_code == 200
    assert resp.content == b""
    assert int(resp.headers["content-length"]) == sample_video.stat().st_size
    assert resp.headers["content-type"] == "video/mp4"


def test_signed_media_range(web_client, web_make_video, sample_video):
    video = web_make_video()
    data = sample_video.read_bytes()
    resp = web_client.get(_url(video.id), headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 0-99/{len(data)}"
    assert resp.content == data[:100]
    tail = web_client.get(_url(video.id), headers={"Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == data[-10:]
    bad = web_client.get(_url(video.id), headers={"Range": f"bytes={len(data) + 10}-"})
    assert bad.status_code == 416


def test_signed_media_invalid_signature_403(web_client, web_make_video):
    video = web_make_video()
    path = _url(video.id)
    tampered = path.replace(f"/m/{video.id}/", f"/m/{video.id + 1}/")
    assert web_client.get(tampered).status_code == 403
    assert web_client.get(path[:-8] + "AAAAAAAA.mp4").status_code == 403
    assert web_client.get(f"/m/{video.id}/notanumber/sig.mp4").status_code == 403


def test_signed_media_expired_410(web_client, web_make_video):
    video = web_make_video()
    path = crypto.signed_media_path(video.id, ttl_seconds=60, now=time.time() - 3600)
    resp = web_client.get(path)
    assert resp.status_code == 410
    assert "noindex" in resp.headers["x-robots-tag"]


def test_signed_media_missing_file_404(web_client, web_make_video, db):
    video = web_make_video()
    storage.abs_path(video.encoded_path).unlink()
    assert web_client.get(_url(video.id)).status_code == 404
    no_file = web_make_video(with_files=False)
    assert web_client.get(_url(no_file.id)).status_code == 404
    assert web_client.get(_url(987654)).status_code == 404


def test_signed_media_refuses_paths_outside_media_root(web_client, web_make_video, db):
    video = web_make_video()
    video.encoded_path = "../../etc/passwd"
    db.commit()
    assert web_client.get(_url(video.id)).status_code == 404


def test_robots_txt(web_client):
    resp = web_client.get("/robots.txt")
    assert resp.status_code == 200
    text = resp.text
    fb_group = text.split("User-agent: facebookexternalhit", 1)[1].split("User-agent: *", 1)[0]
    assert "Allow: /m/" in fb_group and "Disallow: /" in fb_group
    star_group = text.split("User-agent: *", 1)[1]
    assert "Disallow: /" in star_group and "Allow: /m/" not in star_group
    assert "Allow: /privacy" in star_group


def test_thumbnail_and_preview_require_login(web_client, web_make_video):
    video = web_make_video()
    assert web_client.get(f"/library/{video.id}/thumb.jpg", follow_redirects=False).status_code == 303
    assert web_client.get(f"/library/{video.id}/preview", follow_redirects=False).status_code == 303


def test_thumbnail_and_preview_for_owner(web_owner, web_make_video, sample_video):
    video = web_make_video()
    thumb = web_owner.get(f"/library/{video.id}/thumb.jpg")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert thumb.headers["cache-control"] == "private, max-age=300"
    preview = web_owner.get(f"/library/{video.id}/preview", headers={"Range": "bytes=0-9"})
    assert preview.status_code == 206 and preview.content == sample_video.read_bytes()[:10]


def test_thumbnail_and_preview_are_workspace_scoped(web_owner, web_make_video, db):
    other = Workspace(name="Other")
    db.add(other)
    db.commit()
    foreign = web_make_video(workspace_id=other.id)
    assert web_owner.get(f"/library/{foreign.id}/thumb.jpg").status_code == 404
    assert web_owner.get(f"/library/{foreign.id}/preview").status_code == 404
    assert web_owner.get(f"/library/{foreign.id}").status_code == 404


def test_preview_falls_back_to_original(web_owner, web_make_video, db, sample_video):
    video = web_make_video(with_files=False)
    folder = storage.ensure_video_dir(video.workspace_id, video.id)
    original = folder / "original.mov"
    original.write_bytes(sample_video.read_bytes())
    video.original_path = storage.rel_path(original)
    db.commit()
    resp = web_owner.get(f"/library/{video.id}/preview")
    assert resp.status_code == 200 and resp.headers["content-type"] == "video/quicktime"
    assert web_owner.get(f"/library/{video.id}/thumb.jpg").status_code == 404
