"""Operator workflows: freshness, provider preflight and explicit delivery verification."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from repeater import backups, cli, readiness
from repeater.config import reset_settings_cache
from repeater.models import AccountStatus, Job
from repeater.timeutil import utcnow


@pytest.fixture(autouse=True)
def clear_settings():
    yield
    reset_settings_cache()


@pytest.mark.parametrize("value", ["", "junk", "nan", "inf", "-inf", "0", "-1", "2000000000"])
def test_invalid_backup_receipt_never_reports_healthy(tmp_path, value):
    marker = tmp_path / "last_success"
    marker.write_text(value)
    assert not backups.status(marker, now=1_800_000_000)[0]
    assert backups.receipt(marker, now=1_800_000_000).state == "invalid"


def test_backup_receipt_age_boundary_and_missing(tmp_path):
    marker = tmp_path / "last_success"
    assert backups.receipt(marker).state == "missing"
    now = 1_800_000_000
    marker.write_text(str(now - 36 * 3600 + 1))
    assert backups.receipt(marker, now).ok
    marker.write_text(str(now - 36 * 3600))
    assert backups.receipt(marker, now).state == "stale"
    marker.write_text(str(now + 60))
    assert backups.receipt(marker, now).age_hours == 0


def test_backup_phases_report_independently(monkeypatch, tmp_path):
    monkeypatch.setenv("REPEATER_BACKUP_STATUS_FILE", str(tmp_path / "last_success"))
    reset_settings_cache()
    (tmp_path / "last_local_success").write_text(str(time.time()))
    (tmp_path / "last_offsite_db_success").write_text(str(time.time() - 40 * 3600))
    result = backups.receipts()
    assert result["local"]["state"] == "fresh"
    assert result["offsite_db"]["state"] == "stale"
    assert result["offsite_media"]["state"] == "missing"


def test_integration_report_distinguishes_configured_connected_and_acceptance(db, make_account, monkeypatch):
    account = make_account("youtube")
    reconnect = make_account("youtube")
    reconnect.status = AccountStatus.NEEDS_RECONNECT
    db.add(Job(kind="import", payload={"private": "customer-data"}, run_at=utcnow() - timedelta(minutes=20)))
    db.commit()
    monkeypatch.setenv("REPEATER_META_APP_ID", "")
    monkeypatch.setenv("REPEATER_TIKTOK_AUDITED", "false")
    reset_settings_cache()
    result = readiness.report(db, ["youtube", "instagram", "tiktok"])
    youtube, instagram, tiktok = result["platforms"]
    assert youtube["state"] == "connected" and youtube["active_accounts"] == 1
    assert youtube["needs_reconnect"] == 1 and youtube["confirmed_publications"] == 0
    assert youtube["redirect_uri"] == "https://rotator.test/oauth/google/callback"
    assert instagram["state"] == "missing_credentials"
    assert tiktok["state"] == "needs_connection" and tiktok["visibility_limit"] == "private_only"
    assert result["queue"]["oldest_due_age_seconds"] >= 1200
    assert result["live_publishing_verified"] is False
    assert result["publishing_enabled"] is True
    assert result["backups"]["ok"] is False
    serialized = json.dumps(result)
    for secret in (account.token_enc, "g-secret", "tt-secret", "customer-data", account.external_id):
        assert secret not in serialized


def test_publishing_is_locked_by_default_and_reported_independently_of_connection(
    db, make_account, monkeypatch
):
    from repeater.config import Settings

    monkeypatch.delenv("REPEATER_PUBLISHING_ENABLED", raising=False)
    assert Settings(_env_file=None).publishing_enabled is False
    make_account("youtube")
    reset_settings_cache()
    report = readiness.report(db, ["youtube"])
    assert report["platforms"][0]["state"] == "connected"
    assert report["publishing_enabled"] is False


def test_integration_cli_reports_incomplete_without_writes(db, capsys):
    assert cli.main(["integration-status", "--platform", "gdrive"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["platforms"][0]["state"] == "needs_connection"
    assert result["scope"] == "operator_preflight_not_launch_certification"


def test_default_preflight_includes_drive_and_does_not_request_publication(db, monkeypatch):
    from repeater.config import get_settings

    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    result = readiness.report(db)
    entries = {entry["platform"]: entry for entry in result["platforms"]}
    assert set(entries) == {"youtube", "instagram", "facebook", "tiktok", "gdrive"}
    assert entries["gdrive"]["acceptance"] == "read_only_source_test_required"
    assert entries["gdrive"]["visibility_limit"] is None
    for name in ("youtube", "instagram", "facebook", "tiktok"):
        assert entries[name]["acceptance"] == "blocked_by_testing_lock"
        assert entries[name]["visibility_limit"] == "publishing_blocked"
    assert result["telegram"]["delivery"] == "not_configured"


@pytest.mark.parametrize("publishing_enabled,expected", [(False, 0), (True, 1)])
def test_testing_preflight_requires_lock_and_checks_connection_without_alert_delivery(
    db, make_account, monkeypatch, tmp_path, capsys, publishing_enabled, expected
):
    from repeater.config import get_settings

    account = make_account("gdrive")
    settings = get_settings()
    monkeypatch.setattr(settings, "publishing_enabled", publishing_enabled)
    monkeypatch.setattr(settings, "backup_status_file", tmp_path / "last_success")
    for name in (
        "last_success",
        "last_local_success",
        "last_offsite_db_success",
        "last_offsite_media_success",
    ):
        (tmp_path / name).write_text(str(time.time()))
    before = account.token_enc
    assert cli.main(["integration-status", "--testing", "--platform", "gdrive"]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["platforms"][0]["state"] == "connected"
    assert result["publishing_enabled"] is publishing_enabled
    assert not result["telegram"]["configured"] and result["backups"]["ok"]
    db.refresh(account)
    assert account.token_enc == before and not list(db.query(Job))


def test_testing_preflight_does_not_hide_missing_connections(db, monkeypatch, capsys):
    from repeater.config import get_settings

    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    assert cli.main(["integration-status", "--testing"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert all(p["state"] != "connected" for p in result["platforms"])


@pytest.mark.parametrize(
    "missing", ["last_local_success", "last_offsite_db_success", "last_offsite_media_success"]
)
def test_testing_preflight_requires_each_backup_phase(
    db, make_account, monkeypatch, tmp_path, capsys, missing
):
    from repeater.config import get_settings

    make_account("gdrive")
    monkeypatch.setattr(get_settings(), "publishing_enabled", False)
    monkeypatch.setattr(get_settings(), "backup_status_file", tmp_path / "last_success")
    for name in (
        "last_success",
        "last_local_success",
        "last_offsite_db_success",
        "last_offsite_media_success",
    ):
        if name != missing:
            (tmp_path / name).write_text(str(time.time()))
    assert cli.main(["integration-status", "--testing", "--platform", "gdrive"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["backups"]["ok"]  # aggregate receipt alone must not hide an absent phase
    assert any(receipt["state"] == "missing" for receipt in report["backups"]["receipts"].values())


def test_test_alert_requires_destination(monkeypatch, capsys):
    import repeater.notify

    monkeypatch.setattr(repeater.notify, "notify", lambda *a, **kw: pytest.fail("unexpected send"))
    assert cli.main(["test-alert"]) == 2
    assert "not configured" in capsys.readouterr().err


@pytest.mark.parametrize("delivered", [True, False])
def test_test_alert_checks_actual_delivery(monkeypatch, capsys, delivered):
    import repeater.notify

    monkeypatch.setenv("REPEATER_TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("REPEATER_TELEGRAM_CHAT_ID", "123")
    reset_settings_cache()
    calls = []
    monkeypatch.setattr(repeater.notify, "notify", lambda *a, **kw: calls.append((a, kw)) or delivered)
    assert cli.main(["test-alert"]) == (0 if delivered else 1)
    assert calls[0][1] == {"category": "operator_test"}
    output = capsys.readouterr().out
    assert ("delivered" if delivered else "failed") in output
    assert "secret" not in output
