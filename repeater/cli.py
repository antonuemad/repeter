"""Command line: python -m repeater.cli <command>"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

from sqlalchemy import select


def _alembic_cfg():
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


def cmd_migrate(_: argparse.Namespace) -> int:
    from alembic import command

    command.upgrade(_alembic_cfg(), "head")
    print("database is at head")
    return 0


def cmd_gen_secrets(_: argparse.Namespace) -> int:
    from repeater.crypto import generate_key_spec

    print(f"REPEATER_SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"REPEATER_TOKEN_ENCRYPTION_KEYS={generate_key_spec('k1')}")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    from repeater.db import session_scope
    from repeater.models import User, Workspace, WorkspaceMember
    from repeater.security import hash_password

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = args.password or getpass.getpass("Password: ")
    if len(password) < 10:
        print("password must be at least 10 characters", file=sys.stderr)
        return 2
    with session_scope() as s:
        user = s.execute(select(User).where(User.email == args.email.lower())).scalar_one_or_none()
        if user is None:
            user = User(email=args.email.lower(), password_hash=hash_password(password), locale=args.locale)
            s.add(user)
        else:
            user.password_hash = hash_password(password)
        s.flush()
        ws = s.execute(select(Workspace).where(Workspace.name == args.workspace)).scalar_one_or_none()
        if ws is None:
            ws = Workspace(name=args.workspace, timezone=args.timezone, locale=args.locale)
            s.add(ws)
            s.flush()
        member = s.get(WorkspaceMember, (ws.id, user.id))
        if member is None:
            s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        print(f"admin {user.email} is owner of workspace '{ws.name}' (id={ws.id})")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    from repeater.config import get_settings
    from repeater.crypto import KeyRing

    s = get_settings()
    ok = True
    print(f"env={s.env} base_url={s.base_url} media_root={s.media_root}")
    try:
        KeyRing.parse(s.token_encryption_keys)
        print("token encryption keys: ok")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"token encryption keys: INVALID ({exc})")
    if s.is_production and (s.secret_key.startswith("dev-") or len(s.secret_key) < 32):
        ok = False
        print("secret key: INSECURE")
    for p in ("tiktok", "google", "meta"):
        print(
            f"{p}: {'configured' if s.platform_configured(p) else 'not configured'}"
            f" (redirect URI: {s.oauth_redirect_uri(p)})"
        )
    print(f"telegram: {'configured' if s.telegram_bot_token and s.telegram_chat_id else 'not configured'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="repeater")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    sub.add_parser("gen-secrets", help="print new secret key and token encryption key").set_defaults(
        fn=cmd_gen_secrets
    )
    sub.add_parser("check", help="validate configuration").set_defaults(fn=cmd_check)
    ca = sub.add_parser("create-admin", help="create/update the admin user and workspace")
    ca.add_argument("--email", required=True)
    pw = ca.add_mutually_exclusive_group()
    pw.add_argument("--password", help="omit to be prompted (visible to other users in `ps`)")
    pw.add_argument(
        "--password-stdin", action="store_true", help="read the password from the first line of stdin"
    )
    ca.add_argument("--workspace", default="Magic Lab")
    ca.add_argument("--timezone", default="Africa/Cairo")
    ca.add_argument("--locale", default="ar", choices=["ar", "en"])
    ca.set_defaults(fn=cmd_create_admin)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
