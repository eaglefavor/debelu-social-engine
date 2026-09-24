from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from backend.config import settings


_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_LOGIN_ATTEMPTS = 8


def login_is_limited(client_key: str) -> bool:
    now = time.time()
    with _login_lock:
        attempts = _login_attempts[client_key]
        while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= MAX_LOGIN_ATTEMPTS


def note_login_failure(client_key: str) -> None:
    now = time.time()
    with _login_lock:
        attempts = _login_attempts[client_key]
        while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        attempts.append(now)


def clear_login_failures(client_key: str) -> None:
    with _login_lock:
        _login_attempts.pop(client_key, None)


def _decode_base64url(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def make_session_cookie() -> str:
    expires = int(time.time()) + settings.session_hours * 60 * 60
    payload = f"v1:{expires}:{secrets.token_urlsafe(24)}".encode("utf-8")
    signature = hmac.new(settings.app_secret.encode("utf-8"), payload, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded_payload}.{encoded_signature}"


def valid_session_cookie(value: str | None) -> bool:
    if not value:
        return False
    try:
        payload_part, signature_part = value.split(".", 1)
        payload = _decode_base64url(payload_part)
        signature = _decode_base64url(signature_part)
        expected = hmac.new(settings.app_secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        version, expires, nonce = payload.decode("utf-8").split(":", 2)
        return version == "v1" and bool(nonce) and int(expires) > int(time.time())
    except (ValueError, UnicodeDecodeError, TypeError):
        return False
