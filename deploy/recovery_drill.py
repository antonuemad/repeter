#!/usr/bin/env python3
"""Restore off-site DB and media into a network-isolated disposable database, never production."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from monitor import load_env


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=1800, **kwargs)
    if result.returncode:
        raise RuntimeError("Recovery verification command failed: " + str(args[0]))
    return result.stdout


def verify_media(root, rows, *, require_media=False):
    root = Path(root).resolve()
    checked = 0
    fingerprint = hashlib.sha256()
    for row in rows:
        for key in ("original_path", "encoded_path", "thumbnail_path"):
            relative = row.get(key)
            if not relative:
                continue
            path = (root / relative).resolve()
            if Path(relative).is_absolute() or not path.is_relative_to(root) or not path.is_file():
                raise RuntimeError("Restored library has a missing or unsafe media path")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if key == "original_path" and row.get("sha256") and digest.hexdigest() != row["sha256"]:
                raise RuntimeError("Restored original checksum does not match the database")
            expected = (
                row.get("size_bytes" if key == "original_path" else "encoded_size_bytes")
                if key != "thumbnail_path"
                else None
            )
            if expected is not None and path.stat().st_size != expected:
                raise RuntimeError("Restored media size does not match the database")
            fingerprint.update(str(relative).encode() + digest.digest())
            checked += 1
    if require_media and not checked:
        raise RuntimeError("The drill requires actual library media; empty recovery is insufficient")
    return {"referenced_files_verified": checked, "media_fingerprint": fingerprint.hexdigest()}


def verify_canary(sql, media, manifest, values):
    """Compare synthetic records and every derived file with their pre-backup evidence."""
    workspace_id, plan_id = int(manifest["workspace_id"]), int(manifest["plan_id"])
    if sql(f"SELECT paused FROM workspaces WHERE id={workspace_id}") != "t":  # noqa: S608 - integer ID
        raise RuntimeError("Restored canary workspace is missing or unpaused")
    if sql(f"SELECT enabled FROM content_plans WHERE id={plan_id} AND workspace_id={workspace_id}") != "f":  # noqa: S608 - integer IDs
        raise RuntimeError("Restored canary plan is missing or enabled")
    probe = None
    checked = 0
    for item in manifest["items"]:
        row = sql(
            f"SELECT row_to_json(v) FROM videos v WHERE id={int(item['id'])} AND workspace_id={workspace_id}"  # noqa: S608 - integer IDs
        )
        if not row:
            raise RuntimeError("Restored synthetic library item is missing")
        row = json.loads(row)
        for key in ("profile_id", "media_kind", "content_format", "status", "caption"):
            if row.get(key) != item.get(key):
                raise RuntimeError("Restored synthetic library metadata does not match")
        for key, expected in item.get("files", {}).items():
            relative = row.get(key)
            root = Path(media).resolve()
            path = (root / (relative or "")).resolve()
            if (
                not relative
                or Path(relative).is_absolute()
                or not path.is_relative_to(root)
                or not path.is_file()
            ):
                raise RuntimeError("Restored synthetic media checksum does not match")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise RuntimeError("Restored synthetic media checksum does not match")
            checked += 1
        if item.get("encrypted_probe"):
            probe = (row.get("overrides") or {}).get("recovery_encrypted_probe")
    if not probe or not checked:
        raise RuntimeError("The synthetic restore manifest requires media and an encrypted probe")
    # Only the encryption key and the synthetic probe reach an offline Python process. No
    # application/worker or provider client starts, and no secret appears in process arguments.
    answer = run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--entrypoint",
            "python",
            "repeater-app:latest",
            "-c",
            "import json,os,sys; d=json.load(sys.stdin); "
            "os.environ['REPEATER_TOKEN_ENCRYPTION_KEYS']=d['keys']; "
            "from repeater.crypto import decrypt_json; "
            "print(json.dumps(decrypt_json(d['ciphertext'])=={'probe':d['expected']}))",
        ],
        input=json.dumps(
            {
                "keys": values["REPEATER_TOKEN_ENCRYPTION_KEYS"],
                "ciphertext": probe,
                "expected": manifest["probe"],
            }
        ).encode(),
    )
    if answer.strip() != b"true":
        raise RuntimeError("Restored encryption probe could not be decrypted")
    return {
        "canary_items_verified": len(manifest["items"]),
        "canary_files_sha256_verified": checked,
        "encrypted_probe_verified": True,
        "canary_workspace_paused": True,
        "canary_plan_disabled": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--max-media-gb", type=int, default=50)
    parser.add_argument("--require-media", action="store_true", help="fail an empty-library drill")
    parser.add_argument(
        "--canary-manifest", type=Path, help="pre-backup evidence for synthetic records/files"
    )
    args = parser.parse_args(argv)
    values = load_env(args.env_file)
    remote = values.get("RCLONE_REMOTE", "").rstrip("/")
    if not remote or args.max_media_gb < 1:
        raise RuntimeError("Configure an off-site remote and a positive drill size bound")
    environment = dict(os.environ)
    if values.get("RCLONE_CONFIG"):
        environment["RCLONE_CONFIG"] = values["RCLONE_CONFIG"]
    name = "repeater-drill-" + uuid.uuid4().hex[:12]
    started = time.monotonic()
    # Inspect only: never pull an unreviewed image or start any production compose service.
    run(["docker", "image", "inspect", "postgres:16-alpine"])
    listing = json.loads(run(["rclone", "lsjson", remote + "/db", "--files-only"], env=environment))
    dumps = sorted(
        row["Name"]
        for row in listing
        if re.fullmatch(r"repeater-db-[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]+\.sql\.gz", row["Name"])
    )
    if not dumps:
        raise RuntimeError("No off-site database dump found")
    total = json.loads(run(["rclone", "size", remote + "/media", "--json"], env=environment))
    if total["bytes"] > args.max_media_gb * 1024**3:
        raise RuntimeError("Off-site media exceeds the explicit drill size bound")
    with tempfile.TemporaryDirectory(prefix="repeater-recovery-") as directory:
        folder = Path(directory)
        # Preserve at least 2 GiB for production and reserve twice the remote size for transfer overhead.
        if shutil.disk_usage(folder).free < total["bytes"] * 2 + 2 * 1024**3:
            raise RuntimeError("Insufficient free disk space for an isolated recovery drill")
        os.chmod(folder, 0o700)
        dump = folder / "database.sql.gz"
        media = folder / "media"
        run(["rclone", "copyto", remote + "/db/" + dumps[-1], str(dump)], env=environment)
        media.mkdir()
        run(["rclone", "copy", remote + "/media", str(media)], env=environment)
        run(["rclone", "check", remote + "/media", str(media), "--download", "--one-way"], env=environment)
        container_started = False
        try:
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--memory",
                    "768m",
                    "--cpus",
                    "0.5",
                    "--pids-limit",
                    "128",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,size=1073741824",
                    "--tmpfs",
                    "/tmp:rw,size=67108864",  # noqa: S108 - tmpfs inside a disposable network-isolated container
                    "-e",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "-e",
                    "POSTGRES_USER=repeater",
                    "-e",
                    "POSTGRES_DB=restore_drill",
                    "postgres:16-alpine",
                ]
            )
            container_started = True
            for _ in range(50):
                ready = subprocess.run(
                    [
                        "docker",
                        "exec",
                        name,
                        "pg_isready",
                        "-h",
                        "127.0.0.1",
                        "-U",
                        "repeater",
                        "-d",
                        "restore_drill",
                    ],
                    capture_output=True,
                    timeout=10,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Disposable database did not become ready")
            # Stream the dump; subprocess never receives production credentials or starts a worker.
            process = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "psql",
                    "-U",
                    "repeater",
                    "-d",
                    "restore_drill",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "--set=VERBOSITY=sqlstate",
                    "-q",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                with gzip.open(dump, "rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        process.stdin.write(block)
                process.stdin.close()
                process.stdin = None
                _, errors = process.communicate(timeout=180)
                if process.returncode:
                    code = re.search(rb"ERROR:\s*([A-Z0-9]{5})", errors)
                    raise RuntimeError(
                        "Disposable database restore failed ("
                        + (code[1].decode() if code else "process interrupted")
                        + ")"
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

            def sql(query):
                return (
                    run(
                        [
                            "docker",
                            "exec",
                            name,
                            "psql",
                            "-U",
                            "repeater",
                            "-d",
                            "restore_drill",
                            "-At",
                            "-c",
                            query,
                        ]
                    )
                    .decode()
                    .strip()
                )

            sql("UPDATE workspaces SET paused=true; UPDATE content_plans SET enabled=false;")
            rows = [
                json.loads(line)
                for line in sql(
                    "SELECT row_to_json(v) FROM (SELECT original_path,encoded_path,thumbnail_path,sha256,size_bytes,encoded_size_bytes FROM videos) v"
                ).splitlines()
            ]
            receipt = {
                "offsite_dump": dumps[-1],
                "database_restored": True,
                "network": "none",
                "application_started": False,
                "worker_started": False,
                "publishing_enabled": False,
                "workspaces": int(sql("SELECT count(*) FROM workspaces")),
                "library_items": len(rows),
                "offsite_media_files": total["count"],
                **verify_media(media, rows, require_media=args.require_media),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "checked_at": int(time.time()),
            }
            if args.canary_manifest:
                receipt.update(
                    verify_canary(sql, media, json.loads(args.canary_manifest.read_text()), values)
                )
        finally:
            if container_started:
                run(["docker", "rm", "-f", name])
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(receipt, stream, indent=2)
    # Public status mount contains only an epoch, never restored content or credentials.
    marker = args.receipt.parent / "last_recovery_drill_success"
    descriptor, temporary = tempfile.mkstemp(prefix=".recovery-status-", dir=marker.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(str(receipt["checked_at"]) + "\n")
        os.replace(temporary, marker)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
