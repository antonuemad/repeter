"""Real PostgreSQL coverage of manual invitation and recovery flows."""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from repeater import access
from repeater.db import get_sessionmaker
from repeater.models import (
    AccessThrottle,
    PasswordReset,
    RecoveryRequest,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from repeater.security import hash_password, verify_password
from repeater.timeutil import utcnow
from repeater.web import consent
from tests.test_web_support import OWNER_EMAIL, PASSWORD, csrf_from, login, post, set_lang

pytest_plugins = ["tests.test_web_support"]
NEW_PASSWORD = "عبارة مرور طويلة وفريدة 123"
CUSTOMER_EMAIL = "customer@example.com"


@pytest.fixture
def operator(web_owner, db, admin_user):
    admin_user.is_platform_admin = True
    db.commit()
    return web_owner


@pytest.fixture
def customer(db, workspace):
    user = User(
        email=CUSTOMER_EMAIL,
        password_hash=hash_password(PASSWORD),
        terms_version=consent.LEGAL_VERSION,
        terms_accepted_at=utcnow(),
    )
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="editor"))
    db.commit()
    return user


def issued_url(response):
    assert response.status_code == 200, response.text[:1000]
    return html.unescape(re.search(r"<textarea[^>]*>([^<]+)</textarea>", response.text)[1])


def open_private(client, url):
    parts = urlparse(url)
    assert not parts.query and parts.fragment.startswith("token=")
    token = parse_qs(parts.fragment)["token"][0]
    page = client.get(parts.path)
    response = client.post(
        parts.path + "/open",
        data={"token": token, "csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )
    return token, response


def password_form(**overrides):
    return {
        "password": NEW_PASSWORD,
        "confirmation": NEW_PASSWORD,
        "accept": "1",
        "legal_version": consent.LEGAL_VERSION,
        **overrides,
    }


def invite(db, workspace, admin_user, email="new@example.com"):
    row, url = access.issue_invitation(db, workspace.id, email, "editor", admin_user.id)
    db.commit()
    return row, url


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_create_customer_and_complete_invitation(operator, db, web_app, lang):
    set_lang(operator, lang)
    response = post(
        operator, "/admin/customers", {"name": "عميل تجريبي", "email": "NEW@Example.com", "locale": lang}
    )
    url = issued_url(response)
    assert url.startswith("https://rotator.test/invite#token=")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    row = db.execute(select(WorkspaceInvitation)).scalar_one()
    workspace = db.get(Workspace, row.workspace_id)
    assert workspace.paused and workspace.locale == lang and row.role == "owner"
    with TestClient(web_app, base_url="https://rotator.test") as client:
        token, opened = open_private(client, url)
        assert opened.status_code == 303 and opened.headers["location"] == "/invite/accept"
        assert token not in str(client.cookies) and token not in row.token_hash
        assert client.get("/invite/accept").status_code == 200
        result = post(client, "/invite/accept", password_form(), page="/invite/accept")
        assert result.status_code == 303 and result.headers["location"] == "/login"
        assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")
        assert login(client, "new@example.com", NEW_PASSWORD).status_code == 303
        assert client.get("/settings/workspace").status_code == 200
    db.expire_all()
    user = db.execute(select(User).where(User.email == "new@example.com")).scalar_one()
    assert user.terms_accepted_at and user.terms_version == consent.LEGAL_VERSION
    assert not user.is_platform_admin
    assert db.get(WorkspaceInvitation, row.id).used_at
    assert db.get(WorkspaceMember, (workspace.id, user.id)).role == "owner"


@pytest.mark.parametrize(
    "data,error",
    [
        ({"password": "short", "confirmation": "short"}, "password_length"),
        ({"password": "x" * 129, "confirmation": "x" * 129}, "password_length"),
        ({"confirmation": "something else entirely"}, "password_mismatch"),
        ({"accept": ""}, "accept_terms"),
        ({"legal_version": "old"}, "accept_terms"),
    ],
)
def test_invite_password_and_consent_validation(web_client, db, workspace, admin_user, data, error):
    row, url = invite(db, workspace, admin_user)
    open_private(web_client, url)
    response = post(web_client, "/invite/accept", password_form(**data), page="/invite/accept")
    assert response.status_code == 400
    from repeater.web.i18n import AR

    assert AR["access." + error] in response.text
    db.refresh(row)
    assert row.used_at is None
    assert db.execute(select(User).where(User.email == row.email)).first() is None


@pytest.mark.parametrize("state", ["expired", "revoked", "used"])
def test_invite_rejects_invalid_state(web_client, db, workspace, admin_user, state):
    row, url = invite(db, workspace, admin_user)
    if state == "expired":
        row.expires_at = utcnow() - timedelta(seconds=1)
    else:
        setattr(row, state + "_at", utcnow())
    db.commit()
    _, response = open_private(web_client, url)
    assert response.status_code == 400


def test_revocation_after_link_opened_prevents_redemption(web_client, db, workspace, admin_user):
    row, url = invite(db, workspace, admin_user)
    open_private(web_client, url)
    access.revoke_invitation(db, workspace.id, row.id)
    db.commit()
    token = csrf_from(web_client.get("/login").text)
    response = web_client.post("/invite/accept", data={**password_form(), "csrf_token": token})
    assert response.status_code == 400
    assert db.execute(select(User).where(User.email == row.email)).first() is None


def test_existing_account_cannot_be_taken_over_with_invitation(web_client, db, workspace, admin_user):
    row, url = invite(db, workspace, admin_user)
    open_private(web_client, url)
    existing = User(email=row.email, password_hash=hash_password(PASSWORD))
    db.add(existing)
    db.commit()
    response = post(web_client, "/invite/accept", password_form(), page="/invite/accept")
    assert response.status_code == 400
    db.refresh(existing)
    assert verify_password(existing.password_hash, PASSWORD)
    assert db.get(WorkspaceMember, (workspace.id, existing.id)) is None


def test_reissue_cancels_previous_link(db, workspace, admin_user):
    first, _ = invite(db, workspace, admin_user)
    _, second_url = invite(db, workspace, admin_user)
    db.refresh(first)
    assert first.revoked_at
    with pytest.raises(access.AccessError):
        access.invitation_for(db, first.token_hash)
    assert second_url


def test_invitation_concurrent_redemption_has_one_winner(db, workspace, admin_user):
    row, _ = invite(db, workspace, admin_user)
    token_hash = row.token_hash
    hashed = hash_password(NEW_PASSWORD)

    def redeem(_):
        with get_sessionmaker()() as session:
            try:
                access.redeem_invitation(session, token_hash, hashed)
                session.commit()
                return True
            except access.AccessError:
                session.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(redeem, range(2))) == [False, True]
    assert db.execute(select(func.count()).select_from(User).where(User.email == row.email)).scalar_one() == 1


@pytest.mark.parametrize("path", ["/admin/customers", "/admin/recovery"])
def test_workspace_owner_is_not_platform_operator(web_owner, path):
    assert web_owner.get(path).status_code == 403
    assert post(web_owner, "/admin/customers", {"name": "Other", "email": "x@example.com"}).status_code == 403


def test_editor_cannot_invite_revoke_or_recover(web_editor, db, workspace, admin_user):
    row, _ = invite(db, workspace, admin_user)
    for path, payload in [
        ("/settings/members/invite", {"email": "x@example.com"}),
        (f"/settings/members/{row.id}/revoke", {}),
        ("/admin/recovery/1", {"action": "issue", "verified": "1", "current_password": PASSWORD}),
    ]:
        assert post(web_editor, path, payload).status_code == 403
    assert web_editor.get("/settings/members").status_code == 200
    assert "/settings/members/invite" not in web_editor.get("/settings/members").text


def test_owner_invites_editor_only_and_cannot_revoke_cross_tenant(web_owner, db, admin_user):
    response = post(web_owner, "/settings/members/invite", {"email": "x@example.com", "role": "owner"})
    issued_url(response)
    row = db.execute(select(WorkspaceInvitation)).scalar_one()
    assert row.role == "editor"
    other = Workspace(name="Other")
    db.add(other)
    db.flush()
    foreign, _ = access.issue_invitation(db, other.id, "y@example.com", "owner", admin_user.id)
    db.commit()
    assert post(web_owner, f"/settings/members/{foreign.id}/revoke").status_code == 404
    assert post(web_owner, f"/settings/members/{row.id}/revoke").status_code == 303
    db.refresh(row)
    assert row.revoked_at


@pytest.mark.parametrize(
    "path",
    ["/forgot-password", "/invite/open", "/invite/accept", "/reset-password/open", "/reset-password/confirm"],
)
def test_access_posts_require_csrf(web_client, path):
    assert web_client.post(path, data={"email": CUSTOMER_EMAIL, "password": NEW_PASSWORD}).status_code == 403


def test_forgot_response_is_identical_and_never_changes_account(web_client, db, customer):
    original_hash = customer.password_hash
    known = post(web_client, "/forgot-password", {"email": CUSTOMER_EMAIL}, page="/forgot-password")
    unknown = post(web_client, "/forgot-password", {"email": "unknown@example.com"}, page="/forgot-password")
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text
    rows = db.execute(select(RecoveryRequest).order_by(RecoveryRequest.id)).scalars().all()
    assert [access.recovery_email(r) for r in rows] == [CUSTOMER_EMAIL, "unknown@example.com"]
    assert all("@example.com" not in r.email_enc for r in rows)
    db.refresh(customer)
    assert customer.password_hash == original_hash
    assert db.execute(select(PasswordReset)).first() is None


def test_recovery_throttle_persists_across_app_instances(web_client, db, web_app):
    for _ in range(4):
        assert (
            post(
                web_client, "/forgot-password", {"email": CUSTOMER_EMAIL}, page="/forgot-password"
            ).status_code
            == 200
        )
    from repeater.web.app import create_app

    with TestClient(create_app(), base_url="https://rotator.test") as other:
        assert (
            post(other, "/forgot-password", {"email": CUSTOMER_EMAIL}, page="/forgot-password").status_code
            == 200
        )
    assert db.execute(select(func.count()).select_from(RecoveryRequest)).scalar_one() == 3
    assert db.execute(select(AccessThrottle)).first()


def test_manual_recovery_requires_identity_and_reauthentication(operator, db, customer):
    row = access.request_recovery(db, CUSTOMER_EMAIL)
    db.commit()
    for data in (
        {"action": "issue", "current_password": PASSWORD},
        {"action": "issue", "verified": "1", "current_password": "wrong"},
    ):
        response = post(operator, f"/admin/recovery/{row.id}", data)
        assert response.status_code == 303
        assert db.execute(select(PasswordReset)).first() is None
    assert CUSTOMER_EMAIL in operator.get("/admin/recovery").text
    response = post(
        operator,
        f"/admin/recovery/{row.id}",
        {"action": "issue", "verified": "1", "current_password": PASSWORD},
    )
    url = issued_url(response)
    assert "/reset-password#token=" in url
    db.refresh(row)
    assert row.status == "issued" and row.handled_at and row.handled_by_id
    assert (
        post(
            operator,
            f"/admin/recovery/{row.id}",
            {"action": "issue", "verified": "1", "current_password": PASSWORD},
        ).status_code
        == 404
    )


@pytest.mark.parametrize("email", [OWNER_EMAIL, "unknown@example.com"])
def test_web_recovery_never_issues_for_operator_or_unknown(operator, db, email):
    row = access.request_recovery(db, email)
    db.commit()
    response = post(
        operator,
        f"/admin/recovery/{row.id}",
        {"action": "issue", "verified": "1", "current_password": PASSWORD},
    )
    assert response.status_code == 303
    assert db.execute(select(PasswordReset)).first() is None
    assert post(operator, f"/admin/recovery/{row.id}", {"action": "close"}).status_code == 303
    db.refresh(row)
    assert row.status == "closed"


def test_reset_flow_ends_all_previous_sessions_and_does_not_autologin(web_app, db, customer):
    _, url = access.issue_reset(db, customer.id, None)
    db.commit()
    with (
        TestClient(web_app, base_url="https://rotator.test") as first,
        TestClient(web_app, base_url="https://rotator.test") as second,
        TestClient(web_app, base_url="https://rotator.test") as recovery,
    ):
        assert login(first, CUSTOMER_EMAIL).status_code == login(second, CUSTOMER_EMAIL).status_code == 303
        _, opened = open_private(recovery, url)
        assert opened.status_code == 303
        response = post(recovery, "/reset-password/confirm", password_form(), page="/reset-password/confirm")
        assert response.status_code == 303 and response.headers["location"] == "/login"
        for client in (first, second, recovery):
            assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")
        assert login(recovery, CUSTOMER_EMAIL, NEW_PASSWORD).status_code == 303
        assert login(first, CUSTOMER_EMAIL, PASSWORD).status_code == 400
        _, replay = open_private(first, url)
        assert replay.status_code == 400


@pytest.mark.parametrize("state", ["expired", "used", "password_changed", "inactive"])
def test_invalid_reset_states_rejected(web_client, db, customer, state):
    row, url = access.issue_reset(db, customer.id, None)
    if state == "expired":
        row.expires_at = utcnow() - timedelta(seconds=1)
    if state == "used":
        row.used_at = utcnow()
    if state == "password_changed":
        customer.password_hash = hash_password("another long passphrase")
    if state == "inactive":
        customer.is_active = False
    db.commit()
    assert open_private(web_client, url)[1].status_code == 400


def test_reset_reissue_and_password_confirmation(db, customer, web_client):
    old, _ = access.issue_reset(db, customer.id, None)
    db.commit()
    row, url = access.issue_reset(db, customer.id, None)
    db.commit()
    db.refresh(old)
    assert old.used_at
    open_private(web_client, url)
    response = post(
        web_client,
        "/reset-password/confirm",
        password_form(confirmation="does not match at all"),
        page="/reset-password/confirm",
    )
    assert response.status_code == 400
    db.refresh(row)
    assert row.used_at is None
    assert verify_password(customer.password_hash, PASSWORD)


def test_reset_concurrent_redemption_single_winner(db, customer):
    row, _ = access.issue_reset(db, customer.id, None)
    db.commit()
    token_hash, hashed = row.token_hash, hash_password(NEW_PASSWORD)

    def redeem(_):
        with get_sessionmaker()() as session:
            try:
                access.redeem_reset(session, token_hash, hashed)
                session.commit()
                return True
            except access.AccessError:
                session.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(redeem, range(2))) == [False, True]


def test_an_authenticated_user_must_sign_out_before_opening_links(web_owner, db, workspace, admin_user):
    _, url = invite(db, workspace, admin_user)
    assert open_private(web_owner, url)[1].status_code == 400
    assert web_owner.get("/").status_code == 200


def test_proof_expires_and_digest_cannot_be_submitted_as_secret(web_client, db, workspace, admin_user):
    row, _ = invite(db, workspace, admin_user)
    response = post(web_client, "/invite/open", {"token": row.token_hash}, page="/invite")
    assert response.status_code == 400
    assert web_client.get("/invite/accept").status_code == 400


def test_throttle_atomic_and_expires(db):
    now = utcnow()

    def attempt(_):
        with get_sessionmaker()() as session:
            allowed = access.allow_attempt(session, "parallel", "same-ip", 3, timedelta(minutes=1), now=now)
            session.commit()
            return allowed

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 3
    assert access.allow_attempt(
        db, "parallel", "same-ip", 3, timedelta(minutes=1), now=now + timedelta(minutes=2)
    )


def test_retention_removes_only_old_records(db, workspace, admin_user, customer):
    old, _ = invite(db, workspace, admin_user)
    old.expires_at = utcnow() - timedelta(days=8)
    reset, _ = access.issue_reset(db, customer.id, None)
    reset.expires_at = utcnow() - timedelta(days=8)
    request = access.request_recovery(db, CUSTOMER_EMAIL)
    request.created_at = utcnow() - timedelta(days=8)
    access.allow_attempt(db, "old", "ip", 1, timedelta(minutes=1), now=utcnow() - timedelta(days=2))
    db.flush()
    assert access.purge_access_records(db) == 4
    assert db.get(User, customer.id) and db.get(Workspace, workspace.id)


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_settings_has_own_page_and_active_navigation(web_owner, lang):
    set_lang(web_owner, lang)
    response = web_owner.get("/settings/workspace")
    assert response.status_code == 200
    assert 'href="/settings/workspace" aria-current="page"' in response.text
    assert 'action="/settings/workspace"' in response.text
    rules = web_owner.get("/rules").text
    assert 'action="/settings/workspace"' not in rules and "/rules#workspace" not in rules
    assert (
        post(
            web_owner,
            "/settings/workspace",
            {"timezone": "Africa/Cairo", "cross_gap_hours": "24", "horizon_hours": "48"},
            page="/settings/workspace",
        ).headers["location"]
        == "/settings/workspace"
    )


def test_stored_consent_revocation_overrides_cookie(web_owner, db, admin_user):
    assert web_owner.get("/").status_code == 200
    admin_user.terms_accepted_at = None
    admin_user.terms_version = None
    db.commit()
    response = web_owner.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/consent")


def test_operator_can_reinvite_and_revoke_customer_owner(operator, db):
    url = issued_url(post(operator, "/admin/customers", {"name": "Client", "email": "client@example.com"}))
    row = db.execute(select(WorkspaceInvitation)).scalar_one()
    issued_url(post(operator, f"/admin/customers/{row.workspace_id}/invite", {"email": row.email}))
    db.refresh(row)
    assert row.revoked_at
    latest = db.execute(
        select(WorkspaceInvitation).order_by(WorkspaceInvitation.id.desc()).limit(1)
    ).scalar_one()
    assert latest.role == "owner"
    assert (
        post(operator, f"/admin/customers/{latest.workspace_id}/invitations/{latest.id}/revoke").status_code
        == 303
    )
    db.refresh(latest)
    assert latest.revoked_at
    assert url


def test_cli_operator_grant_preserves_password_and_cli_recovery(db, admin_user, capsys):
    from repeater.cli import main

    original = admin_user.password_hash
    assert not admin_user.is_platform_admin
    assert main(["grant-operator", "--email", OWNER_EMAIL]) == 0
    db.refresh(admin_user)
    assert admin_user.is_platform_admin and admin_user.password_hash == original
    capsys.readouterr()
    assert main(["issue-password-reset", "--email", OWNER_EMAIL]) == 0
    url = capsys.readouterr().out.strip()
    token = parse_qs(urlparse(url).fragment)["token"][0]
    row = access.reset_for(db, access.digest(token))
    assert row.user_id == admin_user.id and row.created_by_id is None
    assert main(["grant-operator", "--email", "absent@example.com"]) == 2


def test_customer_creation_rolls_back_workspace_for_existing_email(operator, db):
    before = db.execute(select(func.count()).select_from(Workspace)).scalar_one()
    response = post(operator, "/admin/customers", {"name": "Must roll back", "email": OWNER_EMAIL})
    assert response.status_code == 303
    assert db.execute(select(func.count()).select_from(Workspace)).scalar_one() == before
    assert db.execute(select(WorkspaceInvitation)).first() is None


def test_issued_links_use_configured_origin_not_request_host(operator, db):
    response = post(
        operator,
        "/settings/members/invite",
        {"email": "safe@example.com"},
        headers={"host": "attacker.example"},
    )
    assert issued_url(response).startswith("https://rotator.test/invite#token=")


def test_operator_permission_is_checked_again_on_existing_session(operator, db, admin_user):
    assert operator.get("/admin/customers").status_code == 200
    admin_user.is_platform_admin = False
    db.commit()
    assert operator.get("/admin/customers").status_code == 403
    assert post(operator, "/admin/recovery/1", {"action": "close"}).status_code == 403


def test_open_link_attempts_are_limited(web_client):
    for _ in range(20):
        assert post(web_client, "/invite/open", {"token": "x" * 43}, page="/invite").status_code == 400
    assert post(web_client, "/invite/open", {"token": "x" * 43}, page="/invite").status_code == 429


def test_password_proof_expires_before_submission(web_client, db, workspace, admin_user, monkeypatch):
    row, url = invite(db, workspace, admin_user)
    open_private(web_client, url)
    from repeater.web.routes import access as routes

    future = utcnow() + timedelta(seconds=routes.PROOF_SECONDS + 1)
    monkeypatch.setattr(routes, "utcnow", lambda: future)
    token = csrf_from(web_client.get("/login").text)
    assert web_client.post("/invite/accept", data={**password_form(), "csrf_token": token}).status_code == 400
    db.refresh(row)
    assert row.used_at is None


def test_hashing_busy_does_not_consume_invitation(web_client, db, workspace, admin_user, monkeypatch):
    from contextlib import contextmanager

    from repeater.web.auth import HashingBusy
    from repeater.web.routes import access as routes

    row, url = invite(db, workspace, admin_user)
    open_private(web_client, url)

    @contextmanager
    def busy():
        raise HashingBusy()
        yield

    monkeypatch.setattr(routes, "hash_slot", busy)
    assert post(web_client, "/invite/accept", password_form(), page="/invite/accept").status_code == 503
    db.refresh(row)
    assert row.used_at is None


def test_maintenance_cleans_access_records(db, workspace, admin_user):
    from repeater.jobs.maintenance import run_maintenance

    row, _ = invite(db, workspace, admin_user)
    row.expires_at = utcnow() - timedelta(days=8)
    db.commit()
    counts = run_maintenance(db)
    assert counts["access_records"] == 1
    db.commit()
    assert db.get(WorkspaceInvitation, row.id, populate_existing=True) is None


def test_issue_reset_refreshes_password_version_after_concurrent_change(db, customer):
    # The operator may have loaded the account before another transaction changes its password.
    old_hash = customer.password_hash
    with get_sessionmaker()() as other:
        current = other.get(User, customer.id)
        current.password_hash = hash_password(NEW_PASSWORD)
        other.commit()
    assert customer.password_hash == old_hash
    row, _ = access.issue_reset(db, customer.id, None)
    assert customer.password_hash != old_hash
    assert row.password_version == access.password_version(customer)
    assert access.reset_for(db, row.token_hash).id == row.id


def test_cli_owner_creation_never_grants_platform_authority(db, workspace, capsys):
    from repeater.cli import main

    assert (
        main(
            [
                "create-admin",
                "--email",
                "owner-only@example.com",
                "--password",
                PASSWORD,
                "--workspace",
                workspace.name,
            ]
        )
        == 0
    )
    user = db.execute(select(User).where(User.email == "owner-only@example.com")).scalar_one()
    assert not user.is_platform_admin
    assert db.get(WorkspaceMember, (workspace.id, user.id)).role == "owner"
    capsys.readouterr()


def test_reset_eligibility_is_checked_after_refreshing_locked_user(db, customer):
    with get_sessionmaker()() as other:
        current = other.get(User, customer.id)
        current.is_platform_admin = True
        other.commit()
    assert not customer.is_platform_admin
    with pytest.raises(access.AccessError):
        access.issue_reset(db, customer.id, None)
    db.rollback()
    assert db.execute(select(PasswordReset)).first() is None
