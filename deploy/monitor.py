#!/usr/bin/env python3
"""Host-side checks survive a stopped web/worker. No application dependencies required.

Run every five minutes. Sends only failure categories/recovery to the configured operator;
unchanged delivered states stay quiet. This cannot detect loss of the monitoring host itself.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def fresh(path: Path, hours: int, now: float) -> bool:
    try:
        epoch = float(path.read_text().split()[0])
        return math.isfinite(epoch) and 0 < epoch <= now + 300 and now - epoch < hours * 3600
    except (OSError, ValueError, IndexError):
        return False


def check_https(base_url: str) -> bool:
    parts = urllib.parse.urlsplit(base_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        return False
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/healthz", timeout=15) as response:  # noqa: S310 - HTTPS origin validated above
            return response.status == 200 and json.loads(response.read(4096)) == {"ok": True}
    except (OSError, ValueError):
        return False


def compose(env_file: Path, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        str(env_file.parent),
        "--env-file",
        str(env_file),
        "-f",
        str(env_file.parent / "docker-compose.yml"),
        *args,
    ]


def check_containers(env_file: Path) -> dict[str, bool]:
    states = {name: False for name in ("web", "worker", "db")}
    try:
        result = subprocess.run(
            compose(env_file, "ps", "--all", "--format", "json"),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            return states
        raw = result.stdout.strip()
        rows = (
            json.loads(raw)
            if raw.startswith("[")
            else [json.loads(line) for line in raw.splitlines() if line]
        )
        if not isinstance(rows, list):
            return states
        for row in rows:
            if not isinstance(row, dict):
                continue
            service = row.get("Service")
            if service in states:
                states[service] = row.get("State") == "running" and row.get("Health") == "healthy"
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        pass
    return states


def check_queue(env_file: Path) -> bool:
    # Read-only query through the existing DB container, independent of web availability.
    query = "SELECT count(*) FROM jobs WHERE status='queued' AND run_at < now() - interval '15 minutes'"
    try:
        result = subprocess.run(
            compose(env_file, "exec", "-T", "db", "psql", "-U", "repeater", "-d", "repeater", "-Atc", query),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "0"
    except (OSError, subprocess.TimeoutExpired):
        return False


def collect(env_file: Path, values: dict[str, str], now: float | None = None) -> dict[str, bool]:
    now = time.time() if now is None else now
    status_dir = Path(values.get("REPEATER_BACKUP_DIR") or "/srv/repeater/backups") / "status"
    checks = {"https": check_https(values.get("REPEATER_BASE_URL", "")), **check_containers(env_file)}
    checks["queue"] = check_queue(env_file) if checks["db"] else False
    try:
        usage = shutil.disk_usage(values.get("REPEATER_MEDIA_DIR") or "/srv/repeater/media")
        checks["disk"] = usage.free >= 2 * 1024**3 and usage.used / usage.total < 0.85
    except OSError:
        checks["disk"] = False
    # last_success is kept for compatibility with existing installations. New receipts show
    # whether the failure was local, remote DB or remote media instead of a false overall green.
    local = status_dir / "last_local_success"
    checks["local_backup"] = fresh(local if local.exists() else status_dir / "last_success", 36, now)
    if values.get("RCLONE_REMOTE"):
        checks["offsite_database"] = fresh(status_dir / "last_offsite_db_success", 36, now)
        mode = values.get("RCLONE_MEDIA_SYNC") or "weekly"
        if mode != "off":
            checks["offsite_media"] = mode in ("weekly", "daily") and fresh(
                status_dir / "last_offsite_media_success", 36 if mode == "daily" else 8 * 24, now
            )
    return checks


def send_alert(values: dict[str, str], failures: list[str]) -> bool:
    email_sent = send_email_alert(values, failures)
    token, chat = values.get("REPEATER_TELEGRAM_BOT_TOKEN", ""), values.get("REPEATER_TELEGRAM_CHAT_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"-?[A-Za-z0-9_@]+", chat):
        return email_sent
    text = "Repeater monitor: " + (
        "needs attention: " + ", ".join(failures) if failures else "all monitored services recovered"
    )
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed Telegram API
            payload = json.loads(response.read(65536))
            return (
                response.status == 200 and isinstance(payload, dict) and payload.get("ok") is True
            ) or email_sent
    except (OSError, ValueError):
        # Exception strings and URLs may contain the bot token; never print them.
        return email_sent


def send_email_alert(values: dict[str, str], failures: list[str]) -> bool:
    host = values.get("REPEATER_SMTP_HOST", "")
    username = values.get("REPEATER_SMTP_USERNAME", "")
    try:
        password = (
            base64.b64decode(values["REPEATER_SMTP_PASSWORD_B64"], validate=True).decode()
            if values.get("REPEATER_SMTP_PASSWORD_B64")
            else values.get("REPEATER_SMTP_PASSWORD", "")
        )
    except (ValueError, UnicodeError):
        return False
    if not host or not username or not password:
        return False
    try:
        use_ssl = values.get("REPEATER_SMTP_SSL", "").lower() in ("true", "1", "yes")
        message = EmailMessage()
        message["From"] = values.get("REPEATER_SMTP_FROM", "info@magic-lap.com")
        message["To"] = values.get("REPEATER_OPERATOR_EMAIL", "info@magic-lap.com")
        message["Subject"] = "Repeater monitor: " + ("action needed" if failures else "recovered")
        message.set_content(
            "Repeater: " + (", ".join(failures) if failures else "All monitored checks recovered.")
        )
        factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        options = {"timeout": 15}
        if use_ssl:
            options["context"] = ssl.create_default_context()
        with factory(
            host, int(values.get("REPEATER_SMTP_PORT") or ("465" if use_ssl else "587")), **options
        ) as client:
            if not use_ssl:
                client.starttls(context=ssl.create_default_context())
            client.login(username, password)
            return not client.send_message(message)
    except (OSError, ValueError, smtplib.SMTPException):
        return False


def update_state(path: Path, checks: dict[str, bool], values: dict[str, str], *, notify: bool = True) -> dict:
    failures = sorted(name for name, ok in checks.items() if not ok)
    fingerprint = hashlib.sha256(json.dumps(failures).encode()).hexdigest()
    try:
        previous = json.loads(path.read_text())
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    delivered = previous.get("delivered_fingerprint")
    last_delivered_failed = bool(previous.get("last_delivered_failed"))
    attempted = notify and delivered != fingerprint and (bool(failures) or last_delivered_failed)
    sent = bool(attempted and send_alert(values, failures))
    if sent:
        delivered, last_delivered_failed = fingerprint, bool(failures)
    state = {
        "checked_at": int(time.time()),
        "checks": checks,
        "failures": failures,
        "fingerprint": fingerprint,
        "delivered_fingerprint": delivered,
        "last_delivered_failed": last_delivered_failed,
        "notification_attempted": attempted,
        "notification_delivered": sent,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".monitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=SCRIPT_DIR / ".env")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument(
        "--no-notify", action="store_true", help="collect and record checks without sending a message"
    )
    args = parser.parse_args(argv)
    try:
        values = load_env(args.env_file)
        if os.environ.get("REPEATER_BACKUP_DIR"):
            values["REPEATER_BACKUP_DIR"] = os.environ["REPEATER_BACKUP_DIR"]
        path = (
            args.state_file
            or Path(values.get("REPEATER_BACKUP_DIR") or "/srv/repeater/backups") / "status" / "monitor.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / ".monitor.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"state": "already_running"}))
                return 0
            state = update_state(path, collect(args.env_file, values), values, notify=not args.no_notify)
        print(json.dumps(state, sort_keys=True))
        return 1 if state["failures"] else 0
    except (OSError, ValueError):
        print(
            json.dumps(
                {"state": "monitor_error", "detail": "check configuration and state directory permissions"}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
