#!/usr/bin/env python3
"""Send two labelled test emails through the real monitor, without disrupting any service."""

import argparse
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from monitor import load_env, update_state


def exercise(values, folder, drill_id):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", drill_id):
        raise ValueError("Use a short alphanumeric drill identifier")
    values = dict(
        values,
        REPEATER_MONITOR_DRILL_ID=drill_id,
        REPEATER_TELEGRAM_BOT_TOKEN="",
        REPEATER_TELEGRAM_CHAT_ID="",
    )
    path = Path(folder) / "notification-drill.json"
    if path.exists():
        raise ValueError("A notification drill requires a fresh isolated state file")
    initial = update_state(path, {"simulated_external_https": True}, values)
    failed = update_state(path, {"simulated_external_https": False}, values)
    repeated = update_state(path, {"simulated_external_https": False}, values)
    recovered = update_state(path, {"simulated_external_https": True}, values)
    quiet = update_state(path, {"simulated_external_https": True}, values)
    result = {
        "drill_id": drill_id,
        "simulated": True,
        "production_state_changed": False,
        "initial_healthy_quiet": not initial["notification_attempted"],
        "failure_smtp_accepted": failed["notification_delivered"],
        "duplicate_failure_suppressed": not repeated["notification_attempted"],
        "recovery_smtp_accepted": recovered["notification_delivered"],
        "duplicate_recovery_suppressed": not quiet["notification_attempted"],
        "inbox_delivery": "requires_recipient_verification",
    }
    result["ok"] = all(
        result[key]
        for key in (
            "initial_healthy_quiet",
            "failure_smtp_accepted",
            "duplicate_failure_suppressed",
            "recovery_smtp_accepted",
            "duplicate_recovery_suppressed",
        )
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--id", default=uuid.uuid4().hex)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    values = load_env(args.env_file) if args.env_file else dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="repeater-notification-drill-") as folder:
        result = exercise(values, folder, args.id)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
