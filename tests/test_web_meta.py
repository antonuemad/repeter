"""Meta deauthorize and data-deletion callbacks, and the deletion status page."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from sqlalchemy import select

from repeater.models import AccountStatus, DataDeletionRequest, Job, RotationRule
from tests.test_web_support import set_lang

pytest_plugins = ["tests.test_web_support"]

APP_SECRET = "m-secret"  # REPEATER_META_APP_SECRET in tests/conftest.py


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signed_request(payload: dict, secret: str = APP_SECRET) -> str:
    encoded = _b64(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{_b64(sig)}.{encoded}"


def _payload(user_id: str = "fb-9") -> dict:
    now = int(time.time())
    return {"algorithm": "HMAC-SHA256", "expires": now + 3600, "issued_at": now, "user_id": user_id}


def _meta_accounts(make_account, db, workspace):
    page = make_account("facebook", external_id="page-1", extra={"page_id": "page-1", "fb_user_id": "fb-9"})
    ig = make_account("instagram", external_id="ig-1", extra={"ig_user_id": "ig-1", "fb_user_id": "fb-9"})
    other = make_account(
        "facebook", external_id="page-2", extra={"page_id": "page-2", "fb_user_id": "fb-other"}
    )
    for account in (page, ig, other):
        db.add(
            RotationRule(
                workspace_id=workspace.id,
                connected_account_id=account.id,
                platform=account.platform,
                enabled=True,
            )
        )
    db.commit()
    return page, ig, other


def test_deauthorize_disconnects_matching_accounts(web_client, db, workspace, make_account):
    page, ig, other = _meta_accounts(make_account, db, workspace)
    resp = web_client.post("/meta/deauthorize", data={"signed_request": signed_request(_payload())})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    db.expire_all()
    for account in (page, ig):
        refreshed = db.get(type(account), account.id)
        assert refreshed.status == AccountStatus.DISABLED and refreshed.token_enc is None
    assert db.get(type(other), other.id).status == AccountStatus.ACTIVE
    rules = {r.connected_account_id: r.enabled for r in db.execute(select(RotationRule)).scalars()}
    assert rules == {page.id: False, ig.id: False, other.id: True}
    assert db.execute(select(Job).where(Job.kind == "plan_workspace")).scalars().first() is not None


def test_deauthorize_does_not_call_meta_revoke(web_client, db, workspace, make_account):
    from repeater.platforms import override

    _meta_accounts(make_account, db, workspace)

    class NoRevoke:
        name, uses_pkce = "meta", False

        def revoke(self, ctx):
            raise AssertionError("revoke must not be called for a deauthorize callback")

    with override("oauth", "meta", NoRevoke()):
        resp = web_client.post("/meta/deauthorize", data={"signed_request": signed_request(_payload())})
    assert resp.status_code == 200


def test_deauthorize_unknown_user_is_ok(web_client):
    resp = web_client.post("/meta/deauthorize", data={"signed_request": signed_request(_payload("nobody"))})
    assert resp.status_code == 200


def test_callbacks_reject_bad_signed_requests(web_client, db, workspace, make_account):
    page, _, _ = _meta_accounts(make_account, db, workspace)
    bad = [
        {},
        {"signed_request": ""},
        {"signed_request": "not-a-signed-request"},
        {"signed_request": signed_request(_payload(), secret="wrong-secret")},
        {"signed_request": signed_request({**_payload(), "algorithm": "HMAC-SHA1"})},
        {"signed_request": signed_request({k: v for k, v in _payload().items() if k != "user_id"})},
    ]
    for path in ("/meta/deauthorize", "/meta/data-deletion"):
        for data in bad:
            resp = web_client.post(path, data=data)
            assert resp.status_code == 400, (path, data)
            assert resp.headers["content-type"].startswith("application/json")
    db.expire_all()
    assert db.get(type(page), page.id).status == AccountStatus.ACTIVE
    assert db.execute(select(DataDeletionRequest)).first() is None


def test_callbacks_need_no_session_or_csrf(web_app, db, workspace):
    from fastapi.testclient import TestClient

    with TestClient(web_app) as client:  # brand-new client: no cookies at all
        resp = client.post("/meta/data-deletion", data={"signed_request": signed_request(_payload())})
        assert resp.status_code == 200
        assert "set-cookie" not in resp.headers


def test_data_deletion_creates_request_and_job(web_client, db):
    resp = web_client.post("/meta/data-deletion", data={"signed_request": signed_request(_payload())})
    assert resp.status_code == 200
    body = resp.json()
    code = body["confirmation_code"]
    assert code.isalnum() and len(code) >= 16
    assert body["url"] == f"https://rotator.test/meta/deletion-status/{code}"
    record = db.execute(select(DataDeletionRequest)).scalar_one()
    assert (record.provider, record.external_user_id, record.status) == ("meta", "fb-9", "received")
    job = db.execute(select(Job).where(Job.kind == "delete_user_data")).scalar_one()
    assert job.payload == {"request_id": record.id}


def test_data_deletion_with_monkeypatched_parser(web_client, db, monkeypatch):
    import repeater.platforms.meta as meta

    monkeypatch.setattr(
        meta, "parse_signed_request", lambda value: {"algorithm": "HMAC-SHA256", "user_id": 42}
    )
    resp = web_client.post("/meta/data-deletion", data={"signed_request": "anything"})
    assert resp.status_code == 200
    assert db.execute(select(DataDeletionRequest)).scalar_one().external_user_id == "42"


def test_deletion_status_page(web_client, db):
    resp = web_client.post("/meta/data-deletion", data={"signed_request": signed_request(_payload())})
    code = resp.json()["confirmation_code"]
    set_lang(web_client, "en")
    page = web_client.get(f"/meta/deletion-status/{code}")
    assert page.status_code == 200
    assert code in page.text and "In progress" in page.text
    record = db.execute(select(DataDeletionRequest)).scalar_one()
    record.status = "completed"
    from repeater.timeutil import utcnow

    record.completed_at = utcnow()
    db.commit()
    set_lang(web_client, "ar")
    page = web_client.get(f"/meta/deletion-status/{code}")
    assert "اكتمل" in page.text and 'dir="rtl"' in page.text


def test_deletion_status_unknown_code(web_client):
    assert web_client.get("/meta/deletion-status/doesnotexist123").status_code == 404
    assert web_client.get("/meta/deletion-status/bad%20code").status_code == 404
