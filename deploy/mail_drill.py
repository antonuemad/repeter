#!/usr/bin/env python3
"""Real SMTP/inbox reset acceptance using a disposable DB; no production user is changed.

Run inside the app image with its configured SMTP account. The explicit recipient must be that
same operator mailbox. All identities and sessions are synthetic. Never prints a bearer link,
password, SMTP response or message body. No worker or platform adapter is started.
"""

import argparse
import email
import hashlib
import imaplib
import json
import os
import re
import secrets
import ssl
import time
import uuid
from email import policy
from email.utils import getaddresses
from urllib.parse import urlsplit

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url


def csrf(response):
    match = re.search(r'name="(?:csrf-token|csrf_token)" (?:content|value)="([^"]+)"', response.text)
    if not match:
        raise RuntimeError("Expected CSRF form was not rendered")
    return match[1]


def received_reset(settings, message_id, token_hash, *, imap_host, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with imaplib.IMAP4_SSL(imap_host, 993, ssl_context=ssl.create_default_context(), timeout=15) as box:
            box.login(settings.smtp_username, settings.smtp_credential)
            for folder in ("INBOX", "Junk"):
                status, _ = box.select(folder, readonly=True)
                if status != "OK":
                    continue
                status, data = box.uid("search", None, "HEADER", "Message-ID", f'"{message_id}"')
                if status != "OK" or not data or not data[0]:
                    continue
                for uid in data[0].split()[-3:]:
                    status, parts = box.uid("fetch", uid, "(BODY.PEEK[])")
                    if status != "OK":
                        continue
                    raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
                    if raw is None:
                        continue
                    message = email.message_from_bytes(raw, policy=policy.default)
                    if str(message["Message-ID"]) != message_id:
                        continue
                    sender = {address.lower() for _, address in getaddresses(message.get_all("From", []))}
                    recipients = {address.lower() for _, address in getaddresses(message.get_all("To", []))}
                    if (
                        sender != {settings.smtp_from.lower()}
                        or settings.smtp_username.lower() not in recipients
                    ):
                        continue
                    body = "\n".join(
                        p.get_content() for p in message.walk() if p.get_content_type() == "text/plain"
                    )
                    for token in re.findall(r"/reset-password#token=([A-Za-z0-9_-]{43})", body):
                        if hashlib.sha256(token.encode()).hexdigest() == token_hash:
                            return token, folder
        time.sleep(3)
    raise RuntimeError("The expected reset message did not arrive within the drill timeout")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--imap-host", default="imap.hostinger.com")
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    from fastapi.testclient import TestClient

    from repeater import mail
    from repeater.config import get_settings, reset_settings_cache
    from repeater.crypto import generate_key_spec
    from repeater.db import Base, get_sessionmaker, reset_engine_cache
    from repeater.models import MailOutbox, User, Workspace, WorkspaceMember
    from repeater.security import hash_password
    from repeater.timeutil import utcnow
    from repeater.web import consent
    from repeater.web.app import create_app

    settings = get_settings()
    if settings.publishing_enabled or settings.tiktok_audited or not settings.mail_configured:
        raise RuntimeError("The publishing locks and configured SMTP are required")
    if args.recipient != settings.operator_email or args.recipient != settings.smtp_username:
        raise RuntimeError("Only the configured operator mailbox can receive this drill")
    base_url = settings.base_url
    drill_id = uuid.uuid4().hex
    database = "repeater_mail_drill_" + drill_id
    admin = create_engine(make_url(settings.database_url), isolation_level="AUTOCOMMIT")
    result = {
        "drill_id": drill_id,
        "scope": "isolated_database_real_smtp_and_inbox",
        "production_passwords_changed": False,
        "publishing_enabled": False,
        "platform_calls": 0,
        "database_removed": False,
    }
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        os.environ["REPEATER_DATABASE_URL"] = (
            make_url(settings.database_url).set(database=database).render_as_string(hide_password=False)
        )
        os.environ["REPEATER_TOKEN_ENCRYPTION_KEYS"] = generate_key_spec("drill")
        for key in (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "META_APP_ID",
            "META_APP_SECRET",
            "INSTAGRAM_APP_ID",
            "INSTAGRAM_APP_SECRET",
            "TIKTOK_CLIENT_KEY",
            "TIKTOK_CLIENT_SECRET",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        ):
            os.environ["REPEATER_" + key] = ""
        reset_settings_cache()
        reset_engine_cache()
        from repeater.db import get_engine

        Base.metadata.create_all(get_engine())
        old_password, new_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with get_sessionmaker()() as db:
            workspace = Workspace(name="Synthetic password recovery drill", paused=True)
            user = User(
                email=args.recipient,
                password_hash=hash_password(old_password),
                terms_version=consent.LEGAL_VERSION,
                terms_accepted_at=utcnow(),
            )
            db.add_all([workspace, user])
            db.flush()
            db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
            db.commit()
        send_email = mail.send_email

        def labelled(recipient, subject, body, *, message_id=None):
            return send_email(
                recipient,
                "[TEST] " + subject,
                "Synthetic recovery drill. No production password will change.\n" + body,
                message_id=f"drill-{drill_id}-{message_id}",
            )

        mail.send_email = labelled
        app = create_app()
        with TestClient(app, base_url=base_url) as previous, TestClient(app, base_url=base_url) as client:
            response = previous.post(
                "/login",
                data={
                    "email": args.recipient,
                    "password": old_password,
                    "csrf_token": csrf(previous.get("/login")),
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert previous.get("/settings/workspace", follow_redirects=False).status_code == 200
            response = client.post(
                "/forgot-password",
                data={"email": args.recipient, "csrf_token": csrf(client.get("/forgot-password"))},
            )
            assert response.status_code == 200
            with get_sessionmaker()() as db:
                mail.deliver(db, {})
                row = db.scalar(select(MailOutbox))
                assert row and row.kind == "reset" and row.status == "sent" and row.payload_enc is None
                from repeater.models import PasswordReset

                token_hash = db.get(PasswordReset, row.reset_id).token_hash
                message_id = f"<repeater-drill-{drill_id}-{row.id}@{urlsplit(base_url).hostname}>"
            token, folder = received_reset(settings, message_id, token_hash, imap_host=args.imap_host)
            result.update(
                smtp_accepted=True,
                received_folder=folder,
                inbox_received=folder == "INBOX",
                link_matches_issued_record=True,
                outbox_payload_wiped=True,
            )
            response = client.post(
                "/reset-password/open",
                data={"token": token, "csrf_token": csrf(client.get("/reset-password"))},
                follow_redirects=False,
            )
            assert response.status_code == 303
            response = client.post(
                "/reset-password/confirm",
                data={
                    "password": new_password,
                    "confirmation": new_password,
                    "csrf_token": csrf(client.get("/reset-password/confirm")),
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            result["previous_session_invalidated"] = (
                previous.get("/settings/workspace", follow_redirects=False).status_code == 303
            )
            response = client.post(
                "/reset-password/open",
                data={"token": token, "csrf_token": csrf(client.get("/reset-password"))},
                follow_redirects=False,
            )
            result["used_link_rejected"] = response.status_code == 400
            response = client.post(
                "/login",
                data={
                    "email": args.recipient,
                    "password": old_password,
                    "csrf_token": csrf(client.get("/login")),
                },
                follow_redirects=False,
            )
            result["old_password_rejected"] = response.status_code != 303
            response = client.post(
                "/login",
                data={
                    "email": args.recipient,
                    "password": new_password,
                    "csrf_token": csrf(client.get("/login")),
                },
                follow_redirects=False,
            )
            result["new_password_works"] = (
                response.status_code == 303
                and client.get("/settings/workspace", follow_redirects=False).status_code == 200
            )
            assert all(
                result[key]
                for key in (
                    "previous_session_invalidated",
                    "used_link_rejected",
                    "old_password_rejected",
                    "new_password_works",
                    "inbox_received",
                )
            )
    finally:
        reset_engine_cache()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()
        result["database_removed"] = True
    from pathlib import Path

    path = Path(args.receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - SMTP responses and bearer links must not enter logs
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "detail": "Acceptance drill failed; no secret details logged",
                }
            )
        )
        raise SystemExit(1) from None
