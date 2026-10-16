#!/usr/bin/env python3
"""Public health check run outside the VPS. No customer data or provider credentials."""

import argparse
import json
import os
from pathlib import Path

from monitor import check_https, update_state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://repeater.magic-lap.com")
    parser.add_argument("--state", type=Path, default=Path("external-monitor-state/state.json"))
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)
    state = update_state(
        args.state, {"external_https": check_https(args.url)}, dict(os.environ), notify=not args.no_notify
    )
    # Only operational booleans leave the process. SMTP error responses are never printed.
    print(json.dumps(state, sort_keys=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write(
                "External Repeater HTTPS: " + ("healthy" if not state["failures"] else "FAILED") + "\n"
            )
    return int(bool(state["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
