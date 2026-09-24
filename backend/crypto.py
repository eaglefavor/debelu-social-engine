from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.config import settings


def _key() -> bytes:
    if not settings.token_encryption_key:
        raise RuntimeError("Social token encryption is not configured.")
    return bytes.fromhex(settings.token_encryption_key)


def encrypt_secret(value: str, associated_data: str) -> str:
    if not value:
        return ""
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_key()).encrypt(nonce, value.encode("utf-8"), associated_data.encode("utf-8"))
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"v1.{encode(nonce)}.{encode(encrypted)}"


def decrypt_secret(value: str, associated_data: str) -> str:
    if not value:
        return ""
    try:
        version, nonce_part, data_part = value.split(".", 2)
        if version != "v1":
            raise ValueError("Unsupported encrypted-secret version.")
        decode = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        plaintext = AESGCM(_key()).decrypt(decode(nonce_part), decode(data_part), associated_data.encode("utf-8"))
        return plaintext.decode("utf-8")
    except Exception as exc:
        raise ValueError("Stored provider credential failed authentication; reconnect the social account.") from exc


def session_fingerprint(cookie_value: str | None) -> str:
    if not cookie_value:
        raise ValueError("An authenticated workspace session is required.")
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


def hash_oauth_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def sign_public_asset(asset_id: str, expires_at: int) -> str:
    payload = f"{asset_id}:{expires_at}".encode("utf-8")
    signature = hmac.new(settings.app_secret.encode("utf-8"), b"public-asset-v1:" + payload, hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def verify_public_asset_signature(asset_id: str, token: str, now: int | None = None) -> bool:
    try:
        expiry_text, signature = token.split(".", 1)
        expiry = int(expiry_text)
        current = int(time.time()) if now is None else now
        if expiry <= current or expiry > current + 8 * 24 * 60 * 60:
            return False
        expected = sign_public_asset(asset_id, expiry).split(".", 1)[1]
        return hmac.compare_digest(signature, expected)
    except (TypeError, ValueError):
        return False


def make_public_asset_url(asset_id: str, lifetime_seconds: int = 24 * 60 * 60) -> str:
    if not settings.public_base_url:
        raise RuntimeError("APP_PUBLIC_URL is required for external platforms to fetch media.")
    expiry = int(time.time()) + lifetime_seconds
    token = sign_public_asset(asset_id, expiry)
    return f"{settings.public_base_url}/public/media/{asset_id}?{urlencode({'token': token})}"
