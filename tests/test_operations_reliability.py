"""Recovery media integrity, notification behavior and bounded multi-tenant simulations."""

import hashlib
import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from sqlalchemy.orm import Session

from repeater.content_plans import simulate
from repeater.db import get_engine
from repeater.models import (
    ConnectedAccount,
    ContentPlan,
    CreatorProfile,
    PlanTarget,
    Platform,
    Video,
    VideoStatus,
    Workspace,
)
from repeater.timeutil import utcnow


def load_script(name):
    path = Path("deploy") / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent.resolve()))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_restored_original_encoded_and_thumbnail_are_verified(tmp_path):
    drill = load_script("recovery_drill")
    for name, data in [("original.mp4", b"original"), ("encoded.mp4", b"encoded"), ("thumb.jpg", b"thumb")]:
        (tmp_path / name).write_bytes(data)
    row = dict(
        original_path="original.mp4",
        encoded_path="encoded.mp4",
        thumbnail_path="thumb.jpg",
        sha256=hashlib.sha256(b"original").hexdigest(),
        size_bytes=8,
        encoded_size_bytes=7,
    )
    result = drill.verify_media(tmp_path, [row])
    assert result["referenced_files_verified"] == 3
    (tmp_path / "original.mp4").write_bytes(b"changed!")
    with pytest.raises(RuntimeError, match="checksum"):
        drill.verify_media(tmp_path, [row])


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "missing.mp4"])
def test_recovery_rejects_missing_or_outside_media(tmp_path, path):
    with pytest.raises(RuntimeError, match="unsafe"):
        load_script("recovery_drill").verify_media(tmp_path, [{"encoded_path": path}])


def test_external_monitor_records_outage_and_recovery_without_provider_calls(tmp_path, monkeypatch, capsys):
    monitor = load_script("external_monitor")
    monkeypatch.setattr(monitor, "check_https", lambda url: False)
    state = tmp_path / "state.json"
    assert monitor.main(["--state", str(state), "--no-notify"]) == 1
    assert json.loads(state.read_text())["failures"] == ["external_https"]
    monkeypatch.setattr(monitor, "check_https", lambda url: True)
    assert monitor.main(["--state", str(state), "--no-notify"]) == 0
    assert not json.loads(state.read_text())["notification_delivered"]


def test_monitor_email_uses_verified_tls_and_redacts_failures(monkeypatch):
    monitor = load_script("monitor")
    calls = []

    class FakeSMTP:
        def __init__(self, *a, **kw):
            calls.append(("open", a, kw))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self, **kw):
            calls.append(("tls", kw))

        def login(self, *a):
            calls.append(("login", a))

        def send_message(self, message):
            calls.append(("message", message))
            return {}

    monkeypatch.setattr(monitor.smtplib, "SMTP_SSL", FakeSMTP)
    values = {
        "REPEATER_SMTP_HOST": "smtp.test.invalid",
        "REPEATER_SMTP_USERNAME": "test",
        "REPEATER_SMTP_PASSWORD": "secret",
        "REPEATER_SMTP_SSL": "true",
    }
    assert monitor.send_email_alert(values, ["external_https"])
    assert calls[0][2]["context"].check_hostname
    message = calls[-1][1]
    assert message["To"] == "info@magic-lap.com" and "secret" not in str(message)


def test_twenty_tenants_rotation_capacity_isolated_and_mock_only(db, tmp_path, capsys):
    """Measure local simulation capacity, not live publishing throughput or a production SLA."""
    ids = []
    tomorrow = (utcnow() + timedelta(days=1)).date()
    for index in range(20):
        ws = Workspace(name=f"Synthetic tenant {index}", timezone="UTC", paused=True)
        db.add(ws)
        db.flush()
        for person_index in range(3):
            person = CreatorProfile(workspace_id=ws.id, name=f"Synthetic person {index}/{person_index}")
            db.add(person)
            db.flush()
            plan = ContentPlan(
                workspace_id=ws.id,
                profile_id=person.id,
                timezone="UTC",
                start_date=tomorrow,
                weekdays=list(range(7)),
                times=["12:00"],
                rotation_mode="repeat",
                repeat_after_days=7,
            )
            db.add(plan)
            db.flush()
            ids.append((ws.id, person.id, plan.id))
            for account_index in range(2):
                account = ConnectedAccount(
                    workspace_id=ws.id,
                    profile_id=person.id,
                    platform=Platform.FACEBOOK,
                    external_id=f"synthetic-{index}-{person_index}-{account_index}",
                    display_name="Synthetic page",
                )
                db.add(account)
                db.flush()
                db.add(PlanTarget(workspace_id=ws.id, plan_id=plan.id, account_id=account.id))
            for item_index in range(20):
                db.add(
                    Video(
                        workspace_id=ws.id,
                        profile_id=person.id,
                        title=f"Synthetic {item_index}",
                        media_kind="text",
                        content_format="post",
                        caption="Synthetic text",
                        status=VideoStatus.READY,
                    )
                )
    db.commit()

    def inspect(record):
        ws_id, person_id, plan_id = record
        began = time.monotonic()
        with Session(get_engine()) as session:
            result = simulate(session, session.get(ContentPlan, plan_id))
            for row in result["rows"]:
                for entry in row.entries:
                    assert entry.account.workspace_id == ws_id
                    if entry.video:
                        assert entry.video.workspace_id == ws_id and entry.video.profile_id == person_id
            return time.monotonic() - began

    with respx.mock(assert_all_called=False) as network:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            timings = sorted(pool.map(inspect, ids))
        assert not network.calls
    report = {
        "synthetic_tenants": 20,
        "people": 60,
        "accounts": 120,
        "library_items": 1200,
        "concurrency": 8,
        "simulations": len(timings),
        "total_seconds": round(time.monotonic() - started, 3),
        "p95_seconds": round(timings[int(len(timings) * 0.95) - 1], 3),
        "scope": "Local Postgres rotation preview, no platform requests",
    }
    (tmp_path / "capacity.json").write_text(json.dumps(report))
    print("CAPACITY_REPORT=" + json.dumps(report))


def test_hostinger_setup_validates_before_atomic_write_and_preserves_complex_password(tmp_path, monkeypatch):
    import base64

    configure = load_script("configure_mail")
    target = tmp_path / ".env"
    target.write_text("REPEATER_PUBLISHING_ENABLED=false\nOTHER=unchanged\nREPEATER_SMTP_PASSWORD=old\n")
    password = "synthetic-$-quote'\"-backtick`-unicode-ع"
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, **kw):
            assert host == "smtp.hostinger.com" and port == 465 and kw["context"].check_hostname

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, username, value):
            calls.append((username, value))

    monkeypatch.setattr(configure.smtplib, "SMTP_SSL", FakeSMTP)
    configure.save(target, password)
    settings = dict(line.split("=", 1) for line in target.read_text().splitlines())
    assert calls == [("info@magic-lap.com", password)]
    assert base64.b64decode(settings["REPEATER_SMTP_PASSWORD_B64"]).decode() == password
    assert settings["REPEATER_PUBLISHING_ENABLED"] == "false" and settings["OTHER"] == "unchanged"
    assert "REPEATER_SMTP_PASSWORD" not in settings and target.stat().st_mode & 0o777 == 0o600

    def reject(*a, **kw):
        raise configure.smtplib.SMTPAuthenticationError(535, b"synthetic rejection")

    before = target.read_bytes()
    monkeypatch.setattr(configure.smtplib, "SMTP_SSL", reject)
    with pytest.raises(configure.smtplib.SMTPException):
        configure.save(target, "wrong-password")
    assert target.read_bytes() == before
