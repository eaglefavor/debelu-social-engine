"""Minimal Google Drive v3 client for the side-channel features in ``drive_ops``.

This deliberately avoids the ``google-api-python-client`` dependency: ``httpx``
and ``cryptography`` are already required by this project, and a service-account
JWT grant is small enough to implement directly. Nothing here runs at import
time and no request is made unless ``settings.gdrive_enabled`` is true, so an
unconfigured install behaves exactly as it did before this module existed.

Scope note: a service account has its own empty Drive. It can only see files and
folders that were explicitly shared with its ``client_email``, or that it created
itself. That is what actually bounds this integration's reach, which is why the
default scope is the full Drive scope rather than a narrower one - the sharing
model for a service account is the real access control.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from backend.config import settings

logger = logging.getLogger("debelu.gdrive")

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDE_MIME = "application/vnd.google-apps.presentation"

# Simple multipart upload holds the whole body in memory; above this we ask Drive
# for a resumable session instead, which suits multi-hundred-megabyte media.
RESUMABLE_THRESHOLD_BYTES = 8 * 1024 * 1024
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


class DriveError(RuntimeError):
    """Raised for any Drive failure that the caller cannot retry itself."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def load_service_account(source: str) -> dict[str, Any]:
    """Accept either a path to a key file or the JSON document itself."""
    text = source.strip()
    if not text:
        raise DriveError("GDRIVE_SERVICE_ACCOUNT_JSON is empty.")
    if text.startswith("{"):
        document = text
    else:
        path = Path(text).expanduser()
        if not path.is_file():
            raise DriveError(f"Service-account key file not found: {path}")
        document = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError as exc:
        raise DriveError("GDRIVE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
    for field in ("client_email", "private_key"):
        if not parsed.get(field):
            raise DriveError(f"Service-account JSON is missing '{field}'.")
    return parsed


class ServiceAccountToken:
    """Exchanges a signed RS256 JWT for an access token and caches it in memory."""

    def __init__(self, account: dict[str, Any], scopes: str, transport: httpx.BaseTransport | None = None) -> None:
        self.client_email: str = account["client_email"]
        self.token_uri: str = account.get("token_uri") or TOKEN_ENDPOINT
        self._private_key = serialization.load_pem_private_key(
            account["private_key"].encode("utf-8"), password=None,
        )
        self._scopes = " ".join(item.strip() for item in scopes.split(",") if item.strip())
        self._transport = transport
        self._token: str = ""
        self._expires_at: float = 0.0

    def assertion(self, now: float | None = None) -> str:
        issued = int(now if now is not None else time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
        claims = _b64url(json.dumps({
            "iss": self.client_email,
            "scope": self._scopes,
            "aud": self.token_uri,
            "iat": issued,
            "exp": issued + 3600,
        }, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = self._private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_b64url(signature)}"

    def access_token(self, now: float | None = None) -> str:
        current = now if now is not None else time.time()
        # Refresh a minute early so an in-flight request never presents a stale token.
        if self._token and current < self._expires_at - 60:
            return self._token
        with httpx.Client(timeout=30.0, transport=self._transport) as client:
            response = client.post(self.token_uri, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self.assertion(current),
            })
        if response.status_code != 200:
            raise DriveError(f"Google token exchange failed ({response.status_code}): {response.text[:300]}")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise DriveError("Google token exchange returned no access_token.")
        self._token = token
        self._expires_at = current + float(payload.get("expires_in", 3600))
        return token


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveClient:
    """Thin, retrying wrapper over the Drive v3 REST API."""

    def __init__(self, token: ServiceAccountToken, timeout: float | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self._token = token
        self._timeout = timeout or settings.gdrive_http_timeout_seconds
        self._transport = transport

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
                 content: bytes | None = None, expect_json: bool = True) -> Any:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._token.access_token()  # cached; refreshes only when near expiry
            request_headers = {"Authorization": f"Bearer {self._token._token}", **(headers or {})}
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                    response = client.request(method, url, params=params, headers=request_headers, content=content)
            except httpx.HTTPError as exc:
                last_error = exc
                self._sleep(attempt)
                continue
            if response.status_code in RETRY_STATUSES:
                last_error = DriveError(f"Drive returned {response.status_code}: {response.text[:200]}")
                self._sleep(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code >= 400:
                raise DriveError(f"Drive {method} {url} failed ({response.status_code}): {response.text[:300]}")
            if not expect_json:
                return response.content
            if not response.content:
                return {}
            return response.json()
        raise DriveError(f"Drive request failed after {MAX_ATTEMPTS} attempts: {last_error}")

    @staticmethod
    def _sleep(attempt: int, retry_after: str | None = None) -> None:
        delay = 0.5 * (2 ** attempt)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 30.0))
            except ValueError:
                pass
        time.sleep(delay)

    # -- folders -----------------------------------------------------------

    def find_folder(self, name: str, parent_id: str | None = None) -> str | None:
        parent = parent_id or "root"
        query = (f"name = '{_escape_query_value(name)}' and mimeType = '{FOLDER_MIME}' "
                 f"and '{parent}' in parents and trashed = false")
        payload = self._request("GET", DRIVE_FILES_URL, params={
            "q": query, "fields": "files(id,name)", "pageSize": "10", "supportsAllDrives": "true",
        })
        files = payload.get("files") or []
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        payload = self._request("POST", DRIVE_FILES_URL, params={"fields": "id", "supportsAllDrives": "true"},
                                headers={"Content-Type": "application/json"}, content=json.dumps(body).encode("utf-8"))
        return payload["id"]

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        """Return the id of ``name`` under ``parent_id``, creating it when absent."""
        return self.find_folder(name, parent_id) or self.create_folder(name, parent_id)

    def ensure_path(self, path: str) -> str:
        """Return the id of a nested folder path such as ``backups/database``."""
        parent: str | None = settings.gdrive_root_folder_id or None
        segments = [segment.strip() for segment in path.split("/") if segment.strip()]
        if parent is None:
            parent = self.ensure_folder(settings.gdrive_root_folder_name, None)
            self._share_if_requested(parent)
        for segment in segments:
            parent = self.ensure_folder(segment, parent)
        return parent

    def _share_if_requested(self, folder_id: str) -> None:
        """Service-account files are invisible to humans until shared."""
        if not settings.gdrive_share_with:
            return
        try:
            self.create_permission(folder_id, settings.gdrive_share_with)
        except DriveError as exc:
            logger.warning("Could not share Drive folder %s with %s: %s", folder_id, settings.gdrive_share_with, exc)

    def create_permission(self, file_id: str, email: str, role: str = "writer") -> dict:
        return self._request(
            "POST", f"{DRIVE_FILES_URL}/{file_id}/permissions",
            params={"fields": "id", "sendNotificationEmail": "true", "supportsAllDrives": "true"},
            headers={"Content-Type": "application/json"},
            content=json.dumps({"type": "user", "role": role, "emailAddress": email}).encode("utf-8"),
        )

    # -- files -------------------------------------------------------------

    def list_files(self, parent_id: str, include_folders: bool = False) -> list[dict]:
        conditions = [f"'{parent_id}' in parents", "trashed = false"]
        if not include_folders:
            conditions.append(f"mimeType != '{FOLDER_MIME}'")
        results: list[dict] = []
        page_token: str | None = None
        while True:
            params = {
                "q": " and ".join(conditions),
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum)",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._request("GET", DRIVE_FILES_URL, params=params)
            results.extend(payload.get("files") or [])
            page_token = payload.get("nextPageToken")
            if not page_token:
                return results

    def get_file(self, file_id: str) -> dict:
        return self._request("GET", f"{DRIVE_FILES_URL}/{file_id}", params={
            "fields": "id,name,mimeType,size,modifiedTime,md5Checksum", "supportsAllDrives": "true",
        })

    def upload_bytes(self, name: str, data: bytes, parent_id: str, mime_type: str = "application/octet-stream") -> str:
        if len(data) >= RESUMABLE_THRESHOLD_BYTES:
            return self._upload_resumable(name, data, parent_id, mime_type)
        boundary = f"debelu{int(time.time() * 1000):x}"
        metadata = json.dumps({"name": name, "parents": [parent_id]}).encode("utf-8")
        body = b"".join([
            f"--{boundary}\r\n".encode("ascii"),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            metadata,
            f"\r\n--{boundary}\r\n".encode("ascii"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
            data,
            f"\r\n--{boundary}--\r\n".encode("ascii"),
        ])
        payload = self._request(
            "POST", DRIVE_UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id", "supportsAllDrives": "true"},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        )
        return payload["id"]

    def _upload_resumable(self, name: str, data: bytes, parent_id: str, mime_type: str) -> str:
        metadata = json.dumps({"name": name, "parents": [parent_id]}).encode("utf-8")
        self._token.access_token()
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            initiate = client.post(
                DRIVE_UPLOAD_URL,
                params={"uploadType": "resumable", "fields": "id", "supportsAllDrives": "true"},
                headers={
                    "Authorization": f"Bearer {self._token._token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": mime_type,
                    "X-Upload-Content-Length": str(len(data)),
                },
                content=metadata,
            )
            if initiate.status_code >= 400:
                raise DriveError(f"Drive resumable session failed ({initiate.status_code}): {initiate.text[:200]}")
            session_url = initiate.headers.get("Location")
            if not session_url:
                raise DriveError("Drive did not return a resumable session URL.")
            upload = client.put(
                session_url,
                headers={
                    "Authorization": f"Bearer {self._token._token}",
                    "Content-Type": mime_type,
                    "Content-Length": str(len(data)),
                },
                content=data,
            )
        if upload.status_code >= 400:
            raise DriveError(f"Drive resumable upload failed ({upload.status_code}): {upload.text[:200]}")
        payload = upload.json() if upload.content else {}
        if not payload.get("id"):
            raise DriveError("Drive resumable upload returned no file id.")
        return payload["id"]

    def download_bytes(self, file_id: str) -> bytes:
        return self._request("GET", f"{DRIVE_FILES_URL}/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                             expect_json=False)

    def export_bytes(self, file_id: str, export_mime: str) -> bytes:
        return self._request("GET", f"{DRIVE_FILES_URL}/{file_id}/export", params={"mimeType": export_mime},
                             expect_json=False)

    def read_text(self, file_id: str, mime_type: str | None = None) -> str:
        """Return a Drive file's text: Workspace docs are exported, stored files downloaded."""
        resolved = mime_type or self.get_file(file_id).get("mimeType", "")
        if resolved == GOOGLE_SHEET_MIME:
            raw = self.export_bytes(file_id, "text/csv")
        elif resolved in {GOOGLE_DOC_MIME, GOOGLE_SLIDE_MIME}:
            raw = self.export_bytes(file_id, "text/plain")
        else:
            raw = self.download_bytes(file_id)
        return raw.decode("utf-8", errors="replace")

    def delete_file(self, file_id: str) -> None:
        self._request("DELETE", f"{DRIVE_FILES_URL}/{file_id}", params={"supportsAllDrives": "true"}, expect_json=False)

    def iter_folder(self, parent_id: str) -> Iterator[dict]:
        yield from self.list_files(parent_id)


def is_configured() -> bool:
    return bool(settings.gdrive_enabled and settings.gdrive_service_account_json)


def build_client(transport: httpx.BaseTransport | None = None) -> DriveClient:
    """Construct a client, or raise a message the operator can act on."""
    if not settings.gdrive_enabled:
        raise DriveError("Google Drive integration is disabled. Set GDRIVE_ENABLED=true to use it.")
    account = load_service_account(settings.gdrive_service_account_json)
    token = ServiceAccountToken(account, settings.gdrive_scopes, transport=transport)
    return DriveClient(token, transport=transport)
