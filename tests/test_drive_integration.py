"""Integration coverage for the real DriveClient driving the real ops layer.

``test_gdrive`` stubs the network and ``test_drive_ops`` stubs the client, so a
mismatch between the two - a method the ops layer calls that the client does not
offer, or an upload the client never actually performs - would pass both suites.
These tests wire the genuine ``DriveClient`` to an ``httpx.MockTransport`` and
inspect the bytes that would have reached Drive.
"""

from __future__ import annotations

import importlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, insert

from test_support import TEST_DATA  # noqa: F401
from backend import drive_ops
from backend.config import settings as base_settings
from backend.database import content_ideas, metadata
from backend.gdrive import DriveClient, ServiceAccountToken

SCOPES = "https://www.googleapis.com/auth/drive"
OWNER_EMAIL = "owner@example.test"


def multipart_payload(body: bytes, content_type: str) -> bytes:
    """Pull the file bytes out of a multipart/related upload body."""
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
    parts = body.split(b"--" + boundary.encode("ascii"))
    # parts[0] is empty, parts[1] is the JSON metadata, parts[2] is the file.
    _, _, payload = parts[2].partition(b"\r\n\r\n")
    return payload.rstrip(b"\r\n")


class FakeDrive:
    """Stateful stand-in for the Drive REST API, one instance per test."""

    def __init__(self, seeded_files: list[dict] | None = None):
        self.folders: dict[str, str] = {}
        self.uploads: list[dict] = []
        self.deleted: list[str] = []
        self.permissions: list[dict] = []
        self.token_calls = 0
        self.seeded_files = seeded_files or []
        self._sequence = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.host == "oauth2.example.test":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600})
        if "/permissions" in url:
            self.permissions.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "perm-1"})
        if "uploadType=" in url:
            self.uploads.append({
                "url": url,
                "body": request.content,
                "contentType": request.headers.get("content-type", ""),
            })
            return httpx.Response(200, json={"id": f"uploaded-{len(self.uploads)}"})
        if request.method == "DELETE":
            self.deleted.append(url.rsplit("/", 1)[-1].split("?")[0])
            return httpx.Response(204)
        if request.method == "GET":
            query = request.url.params.get("q") or ""
            if "name = '" in query:
                name = query.split("name = '", 1)[1].split("'", 1)[0]
                found = self.folders.get(name)
                return httpx.Response(200, json={"files": [{"id": found, "name": name}] if found else []})
            parent = query.split("'", 1)[1].split("'", 1)[0]
            seeded = self.seeded_files if self.folders.get("database") == parent else []
            return httpx.Response(200, json={"files": list(seeded)})
        if request.method == "POST":
            payload = json.loads(request.content)
            self._sequence += 1
            folder_id = f"folder-{self._sequence}"
            self.folders[payload["name"]] = folder_id
            return httpx.Response(200, json={"id": folder_id})
        return httpx.Response(404, text=f"unhandled {request.method} {url}")


class DriveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(prefix="debelu-drive-integration-")
        cls.root = Path(cls._temporary.name)
        cls.engine = create_engine(f"sqlite:///{cls.root / 'integration.db'}")
        metadata.create_all(cls.engine)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.account = {
            "client_email": "bot@example.iam.gserviceaccount.com",
            "private_key": key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()).decode("utf-8"),
            "token_uri": "https://oauth2.example.test/token",
        }

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls._temporary.cleanup()

    def setUp(self):
        self.media_dir = self.root / f"media-{self._testMethodName}"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        (self.media_dir / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"p" * 48)
        (self.media_dir / ".upload-pending.tmp").write_bytes(b"partial upload")
        # ``engine`` and ``sqlite_path`` must refer to the same file: the ops layer reads
        # counts through the engine and snapshots the file, so a mismatch would produce a
        # backup that does not match the manifest.
        self.database_path = self.root / f"live-{self._testMethodName}.sqlite"
        self.engine = create_engine(f"sqlite:///{self.database_path}")
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(insert(content_ideas).values(
                id=f"idea-{self._testMethodName}", topic="Integration idea", category="CLOUD",
                audience="Teams", status="IDEA",
                created_at="2026-09-24T10:00:00+00:00", updated_at="2026-09-24T10:00:00+00:00",
            ))

        self.settings = replace(
            base_settings, media_dir=self.media_dir, gdrive_enabled=True,
            gdrive_service_account_json=json.dumps(self.account),
            gdrive_share_with=OWNER_EMAIL, gdrive_backup_retention=2,
        )
        self.stack = ExitStack()
        for module, attribute, value in (
            ("backend.drive_ops", "engine", self.engine),
            ("backend.drive_ops", "settings", self.settings),
            ("backend.gdrive", "settings", self.settings),
        ):
            if hasattr(importlib.import_module(module), attribute):
                self.stack.enter_context(patch(f"{module}.{attribute}", value))
        self.stack.enter_context(patch.object(drive_ops, "sqlite_path", return_value=self.database_path))

    def tearDown(self):
        self.stack.close()
        self.engine.dispose()

    def build_client(self, drive: FakeDrive) -> DriveClient:
        transport = httpx.MockTransport(drive.handler)
        return DriveClient(ServiceAccountToken(self.account, SCOPES, transport=transport), transport=transport)

    def upload_named(self, drive: FakeDrive, suffix: str) -> dict:
        for upload in drive.uploads:
            if suffix in upload["body"][:400].decode("utf-8", errors="replace"):
                return upload
        raise AssertionError(f"no upload whose metadata mentions {suffix}: {[u['body'][:120] for u in drive.uploads]}")

    def test_run_backup_produces_artifacts_drive_can_actually_store(self):
        drive = FakeDrive(seeded_files=[
            {"id": f"old-{index}", "name": f"old-{index}.sqlite", "modifiedTime": f"2026-08-0{index}T00:00:00Z"}
            for index in (1, 2, 3)
        ])
        summary = drive_ops.run_backup(self.build_client(drive), prune=True)

        # The access token is fetched once and reused for every call in the run.
        self.assertEqual(drive.token_calls, 1)
        # Folders are created on demand: imports/ and reports/ only appear when used.
        self.assertEqual(set(drive.folders), {"Debelu Social Engine", "database", "media", "status"})
        # A service account's files are invisible to humans until explicitly shared.
        self.assertTrue(drive.permissions)
        self.assertEqual(drive.permissions[0]["emailAddress"], OWNER_EMAIL)
        # Retention keeps the newest two of the three seeded backups.
        self.assertEqual(drive.deleted, ["old-1"])

        database_upload = self.upload_named(drive, ".sqlite")
        extracted = self.root / "extracted.sqlite"
        extracted.write_bytes(multipart_payload(database_upload["body"], database_upload["contentType"]))
        with sqlite3.connect(extracted) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            topics = connection.execute("SELECT topic FROM content_ideas").fetchall()
        self.assertTrue(drive_ops.REQUIRED_TABLES.issubset(tables), drive_ops.REQUIRED_TABLES - tables)
        self.assertEqual(topics, [("Integration idea",)])
        # The same file the operator would verify later must pass the real inspector.
        self.assertEqual(drive_ops._inspect_sqlite(extracted)["integrity"], "ok")

        media_upload = self.upload_named(drive, ".zip")
        with zipfile.ZipFile(io.BytesIO(multipart_payload(media_upload["body"], media_upload["contentType"]))) as bundle:
            self.assertEqual(bundle.namelist(), ["asset.png"])
        self.assertEqual(summary["media"]["assets"], 1)

        manifest_upload = self.upload_named(drive, ".json")
        manifest = json.loads(multipart_payload(manifest_upload["body"], manifest_upload["contentType"]))
        self.assertEqual(manifest["counts"]["ideas"], 1)
        serialized = json.dumps(manifest).lower()
        for forbidden in ("password", "secret", "token", "private_key"):
            self.assertNotIn(forbidden, serialized)

    def test_backup_then_verify_then_restore_round_trip(self):
        """A backup is only useful if it can be verified and restored afterwards."""
        drive = FakeDrive()
        client = self.build_client(drive)
        drive_ops.backup_database(client)
        uploaded = self.upload_named(drive, ".sqlite")
        archived = multipart_payload(uploaded["body"], uploaded["contentType"])

        # Serve the just-uploaded bytes back for the verify/restore steps. A transport
        # binds its handler at construction, so the client is rebuilt rather than mutated.
        drive.handler = lambda request: self._serve_backup(request, archived)
        client = self.build_client(drive)

        verification = drive_ops.verify_database_backup(client, "backup-1")
        self.assertEqual(verification["integrity"], "ok")
        self.assertIn("content_ideas", verification["tables"])

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM content_ideas")
        result = drive_ops.restore_database(client, "backup-1", confirm=True)
        self.assertTrue(Path(result["safetyCopy"]).is_file())
        with sqlite3.connect(self.database_path) as connection:
            restored = connection.execute("SELECT topic FROM content_ideas").fetchall()
        self.assertEqual(restored, [("Integration idea",)], "the restored database should contain the backed-up row")

    def _serve_backup(self, request: httpx.Request, payload: bytes) -> httpx.Response:
        url = str(request.url)
        if request.url.host == "oauth2.example.test":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "/permissions" in url:
            return httpx.Response(200, json={"id": "perm-1"})
        if request.method == "GET":
            if request.url.params.get("alt") == "media":
                return httpx.Response(200, content=payload)
            return httpx.Response(200, json={"id": "backup-1", "name": "debelu.sqlite",
                                             "mimeType": "application/vnd.sqlite3", "size": str(len(payload))})
        return httpx.Response(200, json={"id": "folder-1"})


if __name__ == "__main__":
    unittest.main()
