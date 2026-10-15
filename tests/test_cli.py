"""repeater.cli: key rotation (gen-secrets, rotate-keys), the configuration check and the backup
status line. Runs against the shared test database (see conftest)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from repeater import backups, cli
from repeater.config import reset_settings_cache
from repeater.crypto import CryptoError, KeyRing, decrypt_json, generate_key_spec
from repeater.models import ConnectedAccount

KEY_ENV = "REPEATER_TOKEN_ENCRYPTION_KEYS"


@pytest.fixture
def use_ring(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Switch REPEATER_TOKEN_ENCRYPTION_KEYS for one test (settings are cached)."""

    def switch(spec: str) -> None:
        monkeypatch.setenv(KEY_ENV, spec)
        reset_settings_cache()

    yield switch
    reset_settings_cache()


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def stored_kid(db, account_id: int) -> str:
    db.expire_all()
    token = db.execute(
        select(ConnectedAccount.token_enc).where(ConnectedAccount.id == account_id)
    ).scalar_one()
    import json

    return json.loads(token)["k"]


# --------------------------------------------------------------------------- gen-secrets


def test_gen_secrets_without_a_ring_prints_both_secrets_with_k1(use_ring, capsys) -> None:
    use_ring("")
    code, out, _ = run_cli(capsys, "gen-secrets")
    assert code == 0
    lines = dict(line.split("=", 1) for line in out.strip().splitlines())
    assert len(lines["REPEATER_SECRET_KEY"]) >= 48
    ring = KeyRing.parse(lines["REPEATER_TOKEN_ENCRYPTION_KEYS"])
    assert ring.active_kid == "k1" and list(ring.keys) == ["k1"]


def test_gen_secrets_with_a_ring_prints_the_rotated_ring(use_ring, capsys) -> None:
    """deploy-ops-10: it always printed kid k1, so `k1:NEW,k1:OLD` silently kept OLD active."""
    old = generate_key_spec("k1")
    use_ring(old)
    code, out, err = run_cli(capsys, "gen-secrets")
    assert code == 0
    assert out.count("\n") == 1 and "REPEATER_SECRET_KEY" not in out
    value = out.strip().split("=", 1)[1]
    ring = KeyRing.parse(value)
    assert list(ring.keys) == ["k2", "k1"] and ring.active_kid == "k2"
    assert ring.keys["k1"] == KeyRing.parse(old).keys["k1"], "the current key is kept after the new one"
    assert "rotate-keys" in err and "secret" in err


def test_gen_secrets_picks_the_next_free_numeric_kid(use_ring, capsys) -> None:
    use_ring(f"{generate_key_spec('k4')},{generate_key_spec('legacy')},{generate_key_spec('k1')}")
    code, out, _ = run_cli(capsys, "gen-secrets")
    assert code == 0
    assert list(KeyRing.parse(out.strip().split("=", 1)[1]).keys) == ["k5", "k4", "legacy", "k1"]


def test_gen_secrets_kid_option_and_with_secret_key(use_ring, capsys) -> None:
    use_ring(generate_key_spec("k1"))
    code, out, _ = run_cli(capsys, "gen-secrets", "--kid", "2027a", "--with-secret-key")
    assert code == 0
    assert "REPEATER_SECRET_KEY=" in out
    assert KeyRing.parse(out.splitlines()[-1].split("=", 1)[1]).active_kid == "2027a"


def test_gen_secrets_refuses_a_kid_in_use_and_bad_rings(use_ring, capsys) -> None:
    use_ring(generate_key_spec("k1"))
    code, out, err = run_cli(capsys, "gen-secrets", "--kid", "k1")
    assert code == 2 and out == "" and "already in the ring" in err
    code, _, err = run_cli(capsys, "gen-secrets", "--kid", "bad kid!")
    assert code == 2 and "key id may only contain" in err
    use_ring(f"{generate_key_spec('k1')},{generate_key_spec('k1')}")
    code, _, err = run_cli(capsys, "gen-secrets")
    assert code == 2 and "repeats key id" in err
    use_ring("k1:not-a-key")
    code, _, err = run_cli(capsys, "gen-secrets")
    assert code == 2 and "invalid" in err


def test_next_kid() -> None:
    assert cli._next_kid([]) == "k1"
    assert cli._next_kid(["k1", "k3"]) == "k4"
    assert cli._next_kid(["main", "k07"]) == "k8"


def test_duplicate_kids_are_found_and_the_keyring_refuses_them() -> None:
    spec = f"{generate_key_spec('k1')},{generate_key_spec('k2')},{generate_key_spec('k1')}"
    assert cli._duplicate_kids(spec) == ["k1"]
    with pytest.raises(CryptoError, match="more than once"):
        KeyRing.parse(spec)


# --------------------------------------------------------------------------- rotate-keys


def test_rotate_keys_rewraps_every_token_so_the_old_key_can_go(
    db, workspace, make_account, use_ring, capsys
) -> None:
    accounts = [
        make_account("tiktok"),
        make_account("youtube"),
        make_account("facebook"),
    ]
    before = [stored_kid(db, a.id) for a in accounts]
    assert set(before) == {"t1"}
    old_spec = os.environ[KEY_ENV]
    new = generate_key_spec("k2")
    use_ring(f"{new},{old_spec}")
    code, out, err = run_cli(capsys, "rotate-keys")
    assert code == 0, err
    assert "re-wrapped: 3" in out and "t1=3" in out and "k2=3" in out
    assert "No stored value uses key(s) t1" in out
    assert {stored_kid(db, a.id) for a in accounts} == {"k2"}
    # With ONLY the new key every token still decrypts: the old key is no longer needed.
    use_ring(new)
    for account in accounts:
        db.refresh(account)
        assert decrypt_json(account.token_enc)["access_token"] == "access-token"


def test_rotate_keys_is_idempotent_and_dry_run_changes_nothing(
    db, workspace, make_account, use_ring, capsys
) -> None:
    account = make_account("tiktok")
    original = account.token_enc
    use_ring(f"{generate_key_spec('k2')},{os.environ[KEY_ENV]}")
    code, out, _ = run_cli(capsys, "rotate-keys", "--dry-run")
    assert code == 0 and "would re-wrap: 1" in out
    db.refresh(account)
    assert account.token_enc == original
    assert run_cli(capsys, "rotate-keys")[0] == 0
    code, out, _ = run_cli(capsys, "rotate-keys")
    assert code == 0 and "re-wrapped: 0" in out
    code, out, _ = run_cli(capsys, "rotate-keys", "--dry-run")
    assert "would re-wrap: 0" in out


def test_rotate_keys_reports_values_it_cannot_read_without_touching_them(
    db, workspace, make_account, use_ring, capsys
) -> None:
    from repeater.crypto import encrypt_json

    make_account("tiktok")
    orphan = make_account("youtube")
    current = os.environ[KEY_ENV]
    # The orphan is sealed with a key that the new ring does not have (dropped too early).
    use_ring(generate_key_spec("lost"))
    orphan.token_enc = encrypt_json({"access_token": "secret-value-must-not-be-printed"})
    db.commit()
    orphan_token = orphan.token_enc
    use_ring(f"{generate_key_spec('k2')},{current}")
    code, out, err = run_cli(capsys, "rotate-keys")
    assert code == 1
    assert f"connected_accounts.token_enc id={orphan.id}" in err and "'lost'" in err
    assert "secret-value-must-not-be-printed" not in out + err
    assert "No stored value uses" not in out
    db.refresh(orphan)
    assert orphan.token_enc == orphan_token, "an unreadable value is left exactly as it was"


def test_rotate_keys_refuses_a_ring_with_a_repeated_kid(use_ring, capsys) -> None:
    use_ring(f"{generate_key_spec('k1')},{generate_key_spec('k1')}")
    code, _, err = run_cli(capsys, "rotate-keys")
    assert code == 2 and "repeats key id" in err
    use_ring("")
    code, _, err = run_cli(capsys, "rotate-keys")
    assert code == 2 and "invalid" in err


def test_rotate_keys_finds_every_encrypted_column_in_the_models() -> None:
    names = {(table.name, column.name) for table, column, _ in cli._encrypted_columns()}
    assert ("connected_accounts", "token_enc") in names


# --------------------------------------------------------------------------- check


def test_check_reports_the_active_key_and_rejects_duplicates(use_ring, capsys) -> None:
    use_ring(f"{generate_key_spec('k2')},{generate_key_spec('k1')}")
    code, out, _ = run_cli(capsys, "check")
    assert code == 0 and "token encryption keys: ok (active k2, 2 key(s))" in out
    use_ring(f"{generate_key_spec('k1')},{generate_key_spec('k1')}")
    code, out, _ = run_cli(capsys, "check")
    assert code == 1 and "INVALID" in out and "['k1']" in out


def test_check_reports_a_stale_backup(use_ring, monkeypatch, tmp_path: Path, capsys) -> None:
    use_ring(generate_key_spec("k1"))
    marker = tmp_path / "last_success"
    monkeypatch.setenv("REPEATER_BACKUP_STATUS_FILE", str(marker))
    code, out, _ = run_cli(capsys, "check")
    assert code == 1 and "backups: no successful backup recorded yet" in out
    marker.write_text(f"{int(time.time() - 3 * 3600)} 2026-10-14T03:17:00Z\n")
    code, out, _ = run_cli(capsys, "check")
    assert code == 0 and "backups: ok (last success 3 hours ago)" in out
    marker.write_text(f"{int(time.time() - 40 * 3600)} 2026-10-12T03:17:00Z\n")
    code, out, _ = run_cli(capsys, "check")
    assert code == 1 and "backups: STALE (the last successful backup was 40 hours ago)" in out


def test_check_without_a_backup_marker_says_nothing_about_backups(use_ring, monkeypatch, capsys) -> None:
    use_ring(generate_key_spec("k1"))
    monkeypatch.delenv("REPEATER_BACKUP_STATUS_FILE", raising=False)
    code, out, _ = run_cli(capsys, "check")
    assert code == 0 and "backups:" not in out


def test_backup_status_parses_garbage_as_no_record(tmp_path: Path) -> None:
    marker = tmp_path / "m"
    marker.write_text("not a number\n")
    assert backups.status(str(marker)) == (
        False,
        "no successful backup recorded yet (backup.sh runs daily at 03:17)",
    )
    assert backups.status(None) == (True, "")
