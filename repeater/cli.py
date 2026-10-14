"""Command line: python -m repeater.cli <command>"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import and_, select, update

KID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


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


def _ring_kids(spec: str) -> list[str]:
    """Key ids in a REPEATER_TOKEN_ENCRYPTION_KEYS value, in order (duplicates kept)."""
    return [part.split(":", 1)[0].strip() for part in spec.split(",") if part.strip()]


def _duplicate_kids(spec: str) -> list[str]:
    counts = Counter(_ring_kids(spec))
    return sorted(kid for kid, n in counts.items() if n > 1)


def _next_kid(kids: list[str]) -> str:
    """k<N+1> after the highest k<N> in use (k1 for an empty ring)."""
    numbers = [int(m.group(1)) for kid in kids if (m := re.fullmatch(r"k(\d+)", kid))]
    return f"k{max(numbers, default=0) + 1}"


def cmd_gen_secrets(args: argparse.Namespace) -> int:
    """Without a configured key ring: a new secret key and a first token key (k1). With one (run
    inside the web container, or with the old value exported): a new token key under the next free
    kid, printed as the complete new ring, new key first and the current keys after it."""
    from repeater.config import get_settings
    from repeater.crypto import CryptoError, KeyRing, generate_key_spec

    current = get_settings().token_encryption_keys.strip()
    if current:
        if _duplicate_kids(current):
            print(
                f"the configured REPEATER_TOKEN_ENCRYPTION_KEYS repeats key id(s) {_duplicate_kids(current)}; "
                "give every key its own id first",
                file=sys.stderr,
            )
            return 2
        try:
            KeyRing.parse(current)
        except (CryptoError, ValueError) as exc:  # a malformed key is a binascii.Error
            print(f"the configured REPEATER_TOKEN_ENCRYPTION_KEYS is invalid: {exc}", file=sys.stderr)
            return 2
    kids = _ring_kids(current)
    kid = args.kid or _next_kid(kids)
    if not KID_RE.fullmatch(kid):
        print("the key id may only contain letters, digits, '_' and '-' (at most 32)", file=sys.stderr)
        return 2
    if kid in kids:
        print(f"the key id {kid!r} is already in the ring; pick another with --kid", file=sys.stderr)
        return 2
    entry = generate_key_spec(kid)
    if args.with_secret_key or not current:
        print(f"REPEATER_SECRET_KEY={secrets.token_urlsafe(48)}")
    if current:
        print(f"REPEATER_TOKEN_ENCRYPTION_KEYS={entry},{current}")
        print(
            "# Rotation: this line holds your CURRENT keys too (treat it as a secret). Put it in deploy/.env,\n"
            "# deploy, run `python -m repeater.cli rotate-keys`, and remove the old key only after it\n"
            "# reports that no value uses it any more.",
            file=sys.stderr,
        )
    else:
        print(f"REPEATER_TOKEN_ENCRYPTION_KEYS={entry}")
    return 0


def _encrypted_columns() -> list[tuple]:
    """Every column named *_enc in the models: (table, column, primary key columns)."""
    import repeater.models  # noqa: F401 - registers the tables
    from repeater.db import Base

    found = []
    for table in Base.metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        found.extend((table, column, pk) for column in table.columns if column.name.endswith("_enc") and pk)
    return found


def _envelope_kid(value: str) -> str:
    try:
        kid = json.loads(value).get("k")
    except Exception:  # noqa: BLE001 - anything unreadable is reported as "?"
        return "?"
    return kid if isinstance(kid, str) else "?"


def cmd_rotate_keys(args: argparse.Namespace) -> int:
    """Re-wrap every stored token envelope with the active key, so old keys can be retired."""
    from repeater.config import get_settings
    from repeater.crypto import CryptoError, KeyRing, decrypt_bytes, encrypt_bytes, needs_rewrap
    from repeater.db import get_sessionmaker

    spec = get_settings().token_encryption_keys
    if _duplicate_kids(spec):
        print(
            f"REPEATER_TOKEN_ENCRYPTION_KEYS repeats key id(s) {_duplicate_kids(spec)}. "
            "Give every key its own id first.",
            file=sys.stderr,
        )
        return 2
    try:
        ring = KeyRing.parse(spec)
    except (CryptoError, ValueError) as exc:  # a malformed key is a binascii.Error
        print(f"REPEATER_TOKEN_ENCRYPTION_KEYS is invalid: {exc}", file=sys.stderr)
        return 2
    sessions = get_sessionmaker()
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    problems: list[str] = []
    rewrapped = 0
    for table, column, pk in _encrypted_columns():
        label = f"{table.name}.{column.name}"
        with sessions() as session:
            ids = session.execute(select(*pk).where(column.is_not(None)).order_by(*pk)).all()
        for row_id in ids:
            where = and_(*(c == v for c, v in zip(pk, row_id, strict=True)))
            # One short transaction per row, locked, so a concurrent token refresh by the worker is
            # never overwritten with a stale value.
            with sessions() as session, session.begin():
                value = session.execute(select(column).where(where).with_for_update()).scalar_one_or_none()
                if not value:
                    continue
                kid = _envelope_kid(value)
                before[kid] += 1
                if not needs_rewrap(value):
                    after[kid] += 1
                    continue
                if args.dry_run:
                    after[kid] += 1
                    rewrapped += 1
                    continue
                try:
                    plain = decrypt_bytes(value)
                except CryptoError:
                    after[kid] += 1
                    problems.append(f"{label} id={'/'.join(str(v) for v in row_id)}: sealed with key {kid!r}")
                    continue
                session.execute(update(table).where(where).values({column.name: encrypt_bytes(plain)}))
                after[ring.active_kid] += 1
                rewrapped += 1

    def fmt(counter: Counter[str]) -> str:
        return ", ".join(f"{k}={n}" for k, n in sorted(counter.items())) or "none"

    print(f"active key: {ring.active_kid}; keys in the ring: {', '.join(ring.keys)}")
    print(f"values by sealing key before: {fmt(before)}")
    print(f"{'would re-wrap' if args.dry_run else 're-wrapped'}: {rewrapped}")
    if not args.dry_run:
        print(f"values by sealing key now: {fmt(after)}")
    if problems:
        print(f"{len(problems)} value(s) could not be read and were left as they are:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        print(
            "Keep (or add back) the key with that id in REPEATER_TOKEN_ENCRYPTION_KEYS and run this again, "
            "or reconnect those accounts in the dashboard.",
            file=sys.stderr,
        )
        return 1
    if not args.dry_run:
        unused = [kid for kid in ring.keys if kid != ring.active_kid and after[kid] == 0]
        if unused:
            print(
                f"No stored value uses key(s) {', '.join(unused)} any more. Remove them from "
                "REPEATER_TOKEN_ENCRYPTION_KEYS once no backup you may still restore needs them "
                "(dumps keep tokens sealed with the key that was active when they were taken)."
            )
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
    from repeater import backups
    from repeater.config import get_settings
    from repeater.crypto import KeyRing

    s = get_settings()
    ok = True
    print(f"env={s.env} base_url={s.base_url} media_root={s.media_root}")
    try:
        if _duplicate_kids(s.token_encryption_keys):
            raise ValueError(f"key id(s) {_duplicate_kids(s.token_encryption_keys)} appear more than once")
        ring = KeyRing.parse(s.token_encryption_keys)
        print(f"token encryption keys: ok (active {ring.active_kid}, {len(ring.keys)} key(s))")
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
    backup_ok, backup_line = backups.status(s.backup_status_file)
    if backup_line:
        print(f"backups: {backup_line}")
    return 0 if ok and backup_ok else 1


def cmd_check_proxy(args: argparse.Namespace) -> int:
    from repeater.proxy import untrusted_proxy_addresses

    try:
        unmatched = untrusted_proxy_addresses(
            args.addresses, os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
        )
    except ValueError:
        print(
            "trusted proxy configuration or address is invalid; use explicit IPs/CIDRs, never *",
            file=sys.stderr,
        )
        return 2
    if unmatched:
        print(
            f"trusted proxy mismatch: {', '.join(unmatched)}; set REPEATER_FORWARDED_ALLOW_IPS to the actual proxy IPs/CIDRs, then recreate web",
            file=sys.stderr,
        )
        return 1
    print("trusted proxy addresses: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="repeater")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    gs = sub.add_parser(
        "gen-secrets",
        help="print a new secret key and token key, or (with a key ring configured) a rotated ring",
    )
    gs.add_argument("--kid", help="key id for the new token key (default: the next free k<N>)")
    gs.add_argument(
        "--with-secret-key", action="store_true", help="also print a new REPEATER_SECRET_KEY when rotating"
    )
    gs.set_defaults(fn=cmd_gen_secrets)
    rk = sub.add_parser("rotate-keys", help="re-wrap every stored token with the active encryption key")
    rk.add_argument("--dry-run", action="store_true", help="only report what would be re-wrapped")
    rk.set_defaults(fn=cmd_rotate_keys)
    sub.add_parser("check", help="validate configuration").set_defaults(fn=cmd_check)
    proxy = sub.add_parser("check-proxy", help="validate proxy source addresses against FORWARDED_ALLOW_IPS")
    proxy.add_argument("addresses", nargs="+")
    proxy.set_defaults(fn=cmd_check_proxy)
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
