"""Token encryption and signed media URLs.

Token encryption uses envelope encryption: each value gets a fresh 256-bit data key (DEK),
the payload is sealed with AES-256-GCM under the DEK, and the DEK is wrapped with the active
key-encryption key (KEK) from REPEATER_TOKEN_ENCRYPTION_KEYS. Rotating the KEK only requires
re-wrapping DEKs; moving the KEK to a cloud KMS later only changes wrap/unwrap.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from repeater.config import get_settings

_ENVELOPE_VERSION = 1


class CryptoError(Exception):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass(frozen=True)
class KeyRing:
    active_kid: str
    keys: dict[str, bytes]

    @classmethod
    def parse(cls, spec: str) -> KeyRing:
        keys: dict[str, bytes] = {}
        order: list[str] = []
        for part in (p.strip() for p in spec.split(",") if p.strip()):
            if ":" not in part:
                raise CryptoError("token key must be 'kid:base64key'")
            kid, b64 = part.split(":", 1)
            if kid in keys:
                # A repeated kid would silently keep the last key as the only one for that id.
                raise CryptoError(f"token key id {kid!r} appears more than once")
            raw = _b64d(b64)
            if len(raw) != 32:
                raise CryptoError(f"token key {kid!r} must decode to 32 bytes")
            keys[kid] = raw
            order.append(kid)
        if not order:
            raise CryptoError("REPEATER_TOKEN_ENCRYPTION_KEYS is empty")
        return cls(active_kid=order[0], keys=keys)


def generate_key_spec(kid: str = "k1") -> str:
    """Return a new 'kid:base64key' entry for REPEATER_TOKEN_ENCRYPTION_KEYS."""
    return f"{kid}:{_b64e(os.urandom(32))}"


def _keyring() -> KeyRing:
    return KeyRing.parse(get_settings().token_encryption_keys)


def encrypt_bytes(plaintext: bytes, *, aad: bytes = b"repeater") -> str:
    ring = _keyring()
    kek = ring.keys[ring.active_kid]
    dek = AESGCM.generate_key(bit_length=256)
    n1, n2 = os.urandom(12), os.urandom(12)
    ct = AESGCM(dek).encrypt(n1, plaintext, aad)
    wrapped = AESGCM(kek).encrypt(n2, dek, ring.active_kid.encode())
    env = {"v": _ENVELOPE_VERSION, "k": ring.active_kid, "w": _b64e(n2 + wrapped), "c": _b64e(n1 + ct)}
    return json.dumps(env, separators=(",", ":"))


def decrypt_bytes(token: str, *, aad: bytes = b"repeater") -> bytes:
    try:
        env = json.loads(token)
        kid = env["k"]
        kek = _keyring().keys[kid]
        w, c = _b64d(env["w"]), _b64d(env["c"])
        dek = AESGCM(kek).decrypt(w[:12], w[12:], kid.encode())
        return AESGCM(dek).decrypt(c[:12], c[12:], aad)
    except KeyError as exc:
        raise CryptoError(f"unknown key id or malformed envelope: {exc}") from exc
    except Exception as exc:  # InvalidTag, ValueError, JSON errors
        raise CryptoError("cannot decrypt value") from exc


def encrypt_json(data: dict) -> str:
    return encrypt_bytes(json.dumps(data, separators=(",", ":"), default=str).encode())


def decrypt_json(token: str) -> dict:
    return json.loads(decrypt_bytes(token))


def needs_rewrap(token: str) -> bool:
    """True if the value was sealed with a KEK other than the active one."""
    try:
        return json.loads(token).get("k") != _keyring().active_kid
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- signed media URLs


def _media_key() -> bytes:
    return hashlib.sha256(b"repeater-media-url:" + get_settings().secret_key.encode()).digest()


def media_signature(video_id: int, expires: int) -> str:
    mac = hmac.new(_media_key(), f"{video_id}:{expires}".encode(), hashlib.sha256).digest()
    return _b64e(mac)


def signed_media_path(
    video_id: int, ttl_seconds: int | None = None, now: float | None = None, *, extension: str = "mp4"
) -> str:
    if extension not in ("mp4", "jpg"):
        raise ValueError("unsupported encoded media type")
    ttl = ttl_seconds if ttl_seconds is not None else get_settings().media_url_ttl_seconds
    expires = int((now if now is not None else time.time()) + ttl)
    return f"/m/{video_id}/{expires}/{media_signature(video_id, expires)}.{extension}"


def signed_media_url(video_id: int, ttl_seconds: int | None = None, *, extension: str = "mp4") -> str:
    """Public HTTPS URL that Instagram/Facebook/TikTok fetch the encoded file from."""
    return get_settings().base_url.rstrip("/") + signed_media_path(video_id, ttl_seconds, extension=extension)


def verify_media_signature(video_id: int, expires: int, sig: str, now: float | None = None) -> str:
    """Return 'ok', 'expired' or 'invalid'."""
    expected = media_signature(video_id, expires)
    # Compare bytes: compare_digest raises TypeError for a str that is not ASCII, and the
    # signature comes straight from a URL.
    if not hmac.compare_digest(expected.encode("ascii"), str(sig).encode("utf-8", "replace")):
        return "invalid"
    if expires < int(now if now is not None else time.time()):
        return "expired"
    return "ok"


def random_token(nbytes: int = 32) -> str:
    return _b64e(os.urandom(nbytes))
