"""Host monitor survives app failures, retries undelivered alerts and avoids repeated noise."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("host_monitor", Path(__file__).parents[1] / "deploy/monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def test_env_is_data_not_shell(tmp_path):
    env = tmp_path / ".env"
    env.write_text('KEY=old\n#KEY=ignored\nKEY="$(do-not-run)"\nexport OTHER=x\nSECRET=hidden\n')
    assert monitor.load_env(env) == {"KEY": "$(do-not-run)", "SECRET": "hidden"}


@pytest.mark.parametrize("value", ["", "nan", "inf", "-1", "0", "garbage", "2000000000"])
def test_invalid_receipts_fail_closed(tmp_path, value):
    marker = tmp_path / "receipt"
    marker.write_text(value)
    assert not monitor.fresh(marker, 36, 1_800_000_000)


def test_collect_detects_failed_phases_and_queue_independently(tmp_path, monkeypatch):
    now = 1_800_000_000
    status = tmp_path / "status"
    status.mkdir()
    for name in ("last_local_success", "last_offsite_db_success"):
        (status / name).write_text(str(now))
    monkeypatch.setattr(monitor, "check_https", lambda url: True)
    monkeypatch.setattr(monitor, "check_containers", lambda path: {"web": True, "worker": False, "db": True})
    monkeypatch.setattr(monitor, "check_queue", lambda path: False)
    monkeypatch.setattr(
        monitor.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=3 * 1024**3, used=1, total=4 * 1024**3),
    )
    values = {"REPEATER_BACKUP_DIR": str(tmp_path), "RCLONE_REMOTE": "crypt:", "RCLONE_MEDIA_SYNC": "daily"}
    checks = monitor.collect(tmp_path / ".env", values, now)
    assert checks == {
        "https": True,
        "web": True,
        "worker": False,
        "db": True,
        "queue": False,
        "disk": True,
        "local_backup": True,
        "offsite_database": True,
        "offsite_media": False,
    }
    (status / "last_offsite_media_success").write_text(str(now - 3 * 24 * 3600))
    values["RCLONE_MEDIA_SYNC"] = "weekly"
    assert monitor.collect(tmp_path / ".env", values, now)["offsite_media"]


def test_alert_deduplication_and_recovery(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "send_alert", lambda values, failures: sent.append(failures) or True)
    path = tmp_path / "monitor.json"
    monitor.update_state(path, {"web": True}, {})
    assert not sent  # initial healthy state stays quiet
    monitor.update_state(path, {"web": False}, {})
    monitor.update_state(path, {"web": False}, {})
    monitor.update_state(path, {"web": True}, {})
    monitor.update_state(path, {"web": True}, {})
    assert sent == [["web"], []]
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_failed_delivery_retries_and_corrupt_state_recovers(tmp_path, monkeypatch):
    path = tmp_path / "monitor.json"
    path.write_text("broken")
    calls = []
    monkeypatch.setattr(monitor, "send_alert", lambda values, failures: calls.append(failures) and False)
    for _ in range(2):
        state = monitor.update_state(path, {"db": False}, {"SECRET": "hidden"})
        assert state["notification_attempted"] and not state["notification_delivered"]
    assert calls == [["db"], ["db"]]
    assert "hidden" not in path.read_text()


@pytest.mark.parametrize(
    "raw",
    [
        "bad JSON",
        "null",
        '[{"Service":"web","State":"exited","Health":"healthy"}]',
        '{"Service":"worker","State":"running","Health":"healthy"}',
    ],
)
def test_container_output_fails_safely(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=raw))
    states = monitor.check_containers(tmp_path / ".env")
    assert not states["web"] and not states["db"]


def test_container_timeout_and_stopped_db_fail_closed(monkeypatch, tmp_path):
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("docker", 20)

    monkeypatch.setattr(monitor.subprocess, "run", timeout)
    assert not any(monitor.check_containers(tmp_path / ".env").values())
    assert not monitor.check_queue(tmp_path / ".env")


@pytest.mark.parametrize(
    "base",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com/?token=secret",
    ],
)
def test_https_rejects_unsafe_or_incorrect_origin(base):
    assert not monitor.check_https(base)


@pytest.mark.parametrize("ok", [True, False])
def test_telegram_requires_api_acknowledgement(monkeypatch, ok):
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps({"ok": ok}).encode()

    def send(request, **kw):
        requests.append(request)
        return Response()

    monkeypatch.setattr(monitor.urllib.request, "urlopen", send)
    values = {"REPEATER_TELEGRAM_BOT_TOKEN": "123:secret", "REPEATER_TELEGRAM_CHAT_ID": "456"}
    assert monitor.send_alert(values, ["db"]) is ok
    assert b"needs+attention%3A+db" in requests[0].data
    assert b"secret" not in requests[0].data


def test_main_no_notify_records_failure_without_sending(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("SECRET=hidden\n")
    monkeypatch.setattr(monitor, "collect", lambda *a: {"web": False})
    monkeypatch.setattr(monitor, "send_alert", lambda *a: pytest.fail("unexpected send"))
    assert (
        monitor.main(["--env-file", str(env), "--state-file", str(tmp_path / "state.json"), "--no-notify"])
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["failures"] == ["web"] and not output["notification_attempted"]
