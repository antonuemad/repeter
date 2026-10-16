#!/usr/bin/env python3
"""Owner-entered Hostinger SMTP setup; validates login and stores no secret in arguments/logs."""

import argparse
import base64
import getpass
import os
import smtplib
import ssl
import tempfile
from pathlib import Path


def save(path, password):
    path = Path(path)
    values = {
        "REPEATER_SMTP_HOST": "smtp.hostinger.com",
        "REPEATER_SMTP_PORT": "465",
        "REPEATER_SMTP_SSL": "true",
        "REPEATER_SMTP_USERNAME": "info@magic-lap.com",
        "REPEATER_SMTP_FROM": "info@magic-lap.com",
        "REPEATER_OPERATOR_EMAIL": "info@magic-lap.com",
        "REPEATER_SMTP_PASSWORD_B64": base64.b64encode(password.encode()).decode(),
    }
    with smtplib.SMTP_SSL(
        "smtp.hostinger.com", 465, context=ssl.create_default_context(), timeout=15
    ) as client:
        client.login("info@magic-lap.com", password)
    lines = [
        line
        for line in path.read_text().splitlines()
        if line.split("=", 1)[0] not in (*values, "REPEATER_SMTP_PASSWORD")
    ]
    descriptor, name = tempfile.mkstemp(prefix=".mail-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(
                "\n".join(lines) + "\n" + "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
            )
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    args = parser.parse_args(argv)
    password = getpass.getpass("Existing Hostinger password for info@magic-lap.com (hidden): ")
    if not password:
        raise SystemExit("No password entered. Nothing changed.")
    try:
        save(args.env_file, password)
    except (OSError, smtplib.SMTPException):
        raise SystemExit("SMTP validation failed. Nothing changed; check the mailbox credentials.") from None
    print("Hostinger SMTP login verified and protected settings saved. No message was sent.")
    print("Recreate web and worker to load the settings. Keep REPEATER_PUBLISHING_ENABLED=false.")


if __name__ == "__main__":
    main()
