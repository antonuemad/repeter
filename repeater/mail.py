"""Encrypted transactional outbox. No secret URLs or SMTP responses enter logs."""

import logging
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import urlparse

from sqlalchemy import select

from repeater.config import get_settings
from repeater.crypto import decrypt_json, encrypt_json
from repeater.jobs.registry import handler
from repeater.models import (
    MailOutbox,
    PasswordReset,
    RecoveryRequest,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from repeater.timeutil import utcnow

log = logging.getLogger("repeater.mail")


def queue_link(db, row, url, kind, email, workspace_id=None):
    db.add(
        MailOutbox(
            kind=kind,
            workspace_id=workspace_id,
            invitation_id=row.id if kind == "invite" else None,
            reset_id=row.id if kind == "reset" else None,
            user_id=row.user_id if kind == "reset" else None,
            payload_enc=encrypt_json({"to": email, "url": url}),
            expires_at=row.expires_at,
        )
    )


def send_email(recipient, subject, body, *, message_id=None):
    from repeater.access import email_address

    settings = get_settings()
    if not settings.mail_configured:
        raise RuntimeError("SMTP not configured")
    message = EmailMessage()
    message["From"] = email_address(settings.smtp_from)
    message["To"] = email_address(recipient)
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = f"<repeater-{message_id}@{urlparse(settings.base_url).hostname}>"
    message.set_content(body)
    factory = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    kwargs = {"timeout": max(1, min(settings.smtp_timeout_seconds, 30))}
    if settings.smtp_ssl:
        kwargs["context"] = ssl.create_default_context()
    with factory(settings.smtp_host, settings.smtp_port, **kwargs) as client:
        if not settings.smtp_ssl:
            client.starttls(context=ssl.create_default_context())
        client.login(settings.smtp_username, settings.smtp_credential)
        rejected = client.send_message(message)
        if rejected:
            raise RuntimeError("Recipient rejected")


def _valid(db, mail, now):
    if mail.expires_at <= now or not mail.payload_enc:
        return False
    if mail.workspace_id:
        ws = db.get(Workspace, mail.workspace_id, populate_existing=True)
        if not ws or ws.closed_at:
            return False
    if mail.invitation_id:
        row = db.get(WorkspaceInvitation, mail.invitation_id, populate_existing=True)
        return bool(row and not row.used_at and not row.revoked_at and row.expires_at > now)
    if mail.reset_id:
        from repeater.access import password_version

        row = db.get(PasswordReset, mail.reset_id, populate_existing=True)
        user = db.get(User, row.user_id, populate_existing=True) if row else None
        return bool(
            row
            and user
            and user.is_active
            and not row.used_at
            and row.expires_at > now
            and row.password_version == password_version(user)
            and db.scalar(
                select(WorkspaceMember.user_id)
                .where(WorkspaceMember.user_id == user.id, WorkspaceMember.role.in_(["owner", "editor"]))
                .limit(1)
            )
            is not None
        )
    return mail.kind == "alert"


def prepare_recoveries(db):
    from repeater.access import issue_reset, recovery_email

    for request in list(
        db.scalars(
            select(RecoveryRequest)
            .where(RecoveryRequest.status == "pending")
            .order_by(RecoveryRequest.id)
            .limit(25)
            .with_for_update(skip_locked=True)
        )
    ):
        if request.created_at < utcnow() - timedelta(minutes=30):
            request.status, request.handled_at = "closed", utcnow()
            continue
        user = db.scalar(select(User).where(User.email == recovery_email(request)))
        membership = (
            db.scalar(
                select(WorkspaceMember)
                .where(WorkspaceMember.user_id == user.id, WorkspaceMember.role.in_(["owner", "editor"]))
                .limit(1)
            )
            if user
            else None
        )
        ws = db.get(Workspace, membership.workspace_id) if membership else None
        if user and user.is_active and not user.is_platform_admin and ws and not ws.closed_at:
            row, url = issue_reset(db, user.id, None)
            queue_link(db, row, url, "reset", user.email, ws.id)
        request.status, request.handled_at = "issued", utcnow()


@handler("deliver_mail")
def deliver(db, payload):
    if not get_settings().mail_configured:
        return
    prepare_recoveries(db)
    db.commit()
    for _ in range(25):
        now = utcnow()
        mail = db.scalar(
            select(MailOutbox)
            .where(MailOutbox.status == "queued", MailOutbox.next_attempt_at <= now)
            .order_by(MailOutbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not mail:
            db.rollback()
            break
        if not _valid(db, mail, now):
            mail.status, mail.payload_enc = "expired", None
            db.commit()
            continue
        try:
            data = decrypt_json(mail.payload_enc)
            subject = {
                "invite": "Your Repeater invitation | دعوتك إلى Repeater",
                "reset": "Reset your Repeater password | استعادة كلمة المرور",
                "alert": "Repeater: action needed | تنبيه",
            }[mail.kind]
            body = f"{data.get('body', '')}\n{data.get('url', '')}\n\nRepeater · Magic Lap\nIf you did not request this, ignore this email.\nلو لم تطلب هذه الرسالة، تجاهلها."
            send_email(data["to"], subject, body, message_id=mail.id)
        except Exception as exc:  # noqa: BLE001 - redact SMTP/server details and bearer URLs
            mail.attempts += 1
            mail.next_attempt_at = now + timedelta(seconds=min(3600, 60 * 2**mail.attempts))
            if mail.attempts >= 6:
                mail.status, mail.payload_enc = "failed", None
            log.warning("Mail delivery deferred (%s)", type(exc).__name__)
        else:
            mail.status, mail.payload_enc, mail.sent_at = "sent", None, now
        db.commit()


def queue_operator_alert(db, category, count=1, workspace_id=None):
    db.add(
        MailOutbox(
            kind="alert",
            workspace_id=workspace_id,
            payload_enc=encrypt_json(
                {
                    "to": get_settings().operator_email,
                    "body": f"{category}: {max(1, int(count))}. Workspace: {workspace_id or 'operator'}.",
                    "url": get_settings().base_url + "/activity",
                }
            ),
            expires_at=utcnow() + timedelta(hours=24),
        )
    )
