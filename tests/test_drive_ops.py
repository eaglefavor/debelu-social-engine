"""Tests for the Drive operations layer.

Each test runs against its own SQLite file and media directory, patched into
every module that holds an ``engine`` reference, so nothing here reads or writes
the shared suite database. No request leaves the process: ``FakeDriveClient``
stands in for the real client.
"""

from __future__ import annotations

import contextlib
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

from PIL import Image
from sqlalchemy import create_engine, insert, select

from test_support import TEST_DATA  # noqa: F401
from backend import drive_ops
from backend.config import settings as base_settings
from backend.database import (
    audit_events,
    content_assets,
    content_ideas,
    content_variants,
    metadata,
    publish_jobs,
)
from backend.gdrive import DriveError

GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def png_bytes(size: int = 128, colour: tuple[int, int, int] = (12, 34, 56)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def mp4_bytes(payload: bytes = b"\x00" * 64) -> bytes:
    # backend/media.py sniffs 'ftyp' at offset 4; without ffprobe the duration is None.
    return b"\x00\x00\x00\x20ftypisom" + payload


class FakeDriveClient:
    """In-memory stand-in for DriveClient covering the methods the ops layer uses."""

    def __init__(self, files: dict[str, dict] | None = None, texts: dict[str, str] | None = None):
        self.files = files or {}
        self.texts = texts or {}
        self.uploads: list[dict] = []
        self.deleted: list[str] = []
        self.folders: dict[str, str] = {}
        self._folder_counter = 0
        self._file_counter = 0

    # folders
    def ensure_path(self, path: str) -> str:
        key = path or "root"
        if key not in self.folders:
            self._folder_counter += 1
            self.folders[key] = f"folder-{self._folder_counter}"
        return self.folders[key]

    # files
    def list_files(self, parent_id: str) -> list[dict]:
        return [dict(item) for item in self.files.get(parent_id, [])]

    def get_file(self, file_id: str) -> dict:
        for item in self.files.get("_all", []):
            if item["id"] == file_id:
                return dict(item)
        raise DriveError(f"unknown file {file_id}")

    def read_text(self, file_id: str, mime_type: str | None = None) -> str:
        return self.texts[file_id]

    def download_bytes(self, file_id: str) -> bytes:
        for item in self.files.get("_all", []):
            if item["id"] == file_id:
                return item["_bytes"]
        raise DriveError(f"unknown file {file_id}")

    def upload_bytes(self, name: str, data: bytes, parent_id: str, mime_type: str = "application/octet-stream") -> str:
        self._file_counter += 1
        file_id = f"uploaded-{self._file_counter}"
        self.uploads.append({"id": file_id, "name": name, "parent": parent_id,
                             "bytes": len(data), "mimeType": mime_type, "data": data})
        return file_id

    def delete_file(self, file_id: str) -> None:
        self.deleted.append(file_id)


class DriveOpsTestCase(unittest.TestCase):
    """Shared isolated database, media directory and settings patches."""

    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(prefix="debelu-drive-ops-")
        cls.root = Path(cls._temporary.name)
        cls.engine = create_engine(f"sqlite:///{cls.root / 'ops.db'}")
        metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls._temporary.cleanup()

    def setUp(self):
        self.media_dir = self.root / f"media-{self._testMethodName}"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.settings = replace(
            base_settings,
            media_dir=self.media_dir,
            gdrive_backup_retention=2,
            gdrive_enabled=True,
            gdrive_service_account_json='{"client_email":"bot@example.test","private_key":"x"}',
        )
        self.stack = ExitStack()
        # Modules bind ``engine``/``settings`` as module attributes, so patching each
        # one keeps these tests off the shared suite database and media directory.
        # Only attributes that actually exist are patched (analytics, for instance,
        # never imports settings).
        for module, attribute, value in (
            ("backend.drive_ops", "engine", self.engine),
            ("backend.media", "engine", self.engine),
            ("backend.analytics", "engine", self.engine),
            ("backend.drive_ops", "settings", self.settings),
            ("backend.media", "settings", self.settings),
            ("backend.drive_cli", "settings", self.settings),
            ("backend.gdrive", "settings", self.settings),
        ):
            imported = importlib.import_module(module)
            if hasattr(imported, attribute):
                self.stack.enter_context(patch(f"{module}.{attribute}", value))
        # Start each test from an empty workspace.
        with self.engine.begin() as connection:
            for table in (content_assets, content_variants, content_ideas, audit_events):
                connection.execute(table.delete())

    def tearDown(self):
        self.stack.close()

    def ideas(self) -> list[dict]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(content_ideas)).mappings().all()]

    def variants_for(self, idea_id: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(content_variants).where(content_variants.c.idea_id == idea_id)).mappings().all()
        return [dict(row) for row in rows]

    def audit_actions(self) -> list[str]:
        with self.engine.connect() as connection:
            return [row[0] for row in connection.execute(select(audit_events.c.action)).all()]


class IdeaParsingTests(DriveOpsTestCase):
    def test_parses_rows_and_platform_bodies(self):
        text = (
            "topic,category,audience,instagram,threads,tiktok\n"
            "Zero trust basics,CLOUD,Platform teams,IG body,Threads body,\n"
            "Threat modelling,,,\n"
        )
        ideas = drive_ops.parse_ideas_csv(text)
        self.assertEqual(len(ideas), 2)
        first = ideas[0]
        self.assertEqual(first["topic"], "Zero trust basics")
        self.assertEqual(first["category"], "CLOUD")
        self.assertEqual(first["audience"], "Platform teams")
        self.assertEqual(first["variants"]["instagram"]["body"], "IG body")
        self.assertNotIn("tiktok", first["variants"])
        # Unknown/blank category falls back rather than rejecting the whole sheet.
        self.assertEqual(ideas[1]["category"], drive_ops.DEFAULT_CATEGORY)

    def test_unknown_category_falls_back(self):
        ideas = drive_ops.parse_ideas_csv("topic,category\nSomething,NOT A REAL CATEGORY\n")
        self.assertEqual(ideas[0]["category"], drive_ops.DEFAULT_CATEGORY)

    def test_missing_topic_column_is_rejected(self):
        with self.assertRaises(DriveError) as caught:
            drive_ops.parse_ideas_csv("name,owner\nThing,Me\n")
        self.assertIn("topic", str(caught.exception))

    def test_blank_topics_are_skipped(self):
        self.assertEqual(drive_ops.parse_ideas_csv("topic\n\n   \n"), [])

    def test_long_topic_is_truncated_to_column_width(self):
        ideas = drive_ops.parse_ideas_csv("topic\n" + "x" * 400 + "\n")
        self.assertEqual(len(ideas[0]["topic"]), 240)


class IdeaImportTests(DriveOpsTestCase):
    def client_for(self, csv_text: str) -> FakeDriveClient:
        return FakeDriveClient(
            files={"_all": [{"id": "sheet-1", "name": "Ideas", "mimeType": GOOGLE_SHEET_MIME}]},
            texts={"sheet-1": csv_text},
        )

    def test_import_creates_ideas_variants_and_an_audit_entry(self):
        client = self.client_for("topic,category,audience,threads\nRow A,CLOUD,Teams,Threads body\n")
        result = drive_ops.import_ideas(client, "sheet-1")
        self.assertEqual((result["created"], result["skipped"]), (1, 0))
        stored = self.ideas()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["status"], "IDEA")
        self.assertEqual(stored[0]["category"], "CLOUD")
        # One variant row per platform, matching the API's creation path.
        variants = self.variants_for(stored[0]["id"])
        self.assertEqual({row["platform"] for row in variants}, {"instagram", "threads", "tiktok"})
        self.assertEqual(next(r["body"] for r in variants if r["platform"] == "threads"), "Threads body")
        self.assertIn("drive_ideas_imported", self.audit_actions())

    def test_reimport_is_idempotent_by_topic(self):
        client = self.client_for("topic\nRepeat me\n")
        self.assertEqual(drive_ops.import_ideas(client, "sheet-1")["created"], 1)
        second = drive_ops.import_ideas(client, "sheet-1")
        self.assertEqual((second["created"], second["skipped"]), (0, 1))
        self.assertEqual(len(self.ideas()), 1, "a repeat import must not duplicate ideas")

    def test_import_only_adds_new_topics(self):
        self.assertEqual(drive_ops.import_ideas(self.client_for("topic\nFirst\n"), "sheet-1")["created"], 1)
        result = drive_ops.import_ideas(self.client_for("topic\nFirst\nSecond\n"), "sheet-1")
        self.assertEqual((result["created"], result["skipped"]), (1, 1))
        self.assertEqual(len(self.ideas()), 2)

    def test_dry_run_reports_without_writing(self):
        client = self.client_for("topic\nPlanning only\n")
        result = drive_ops.import_ideas(client, "sheet-1", dry_run=True)
        self.assertEqual(result["created"], 1)
        self.assertTrue(result["dryRun"])
        self.assertEqual(self.ideas(), [])
        self.assertNotIn("drive_ideas_imported", self.audit_actions())


class MediaIntakeTests(DriveOpsTestCase):
    def client_with(self, items: list[dict]) -> FakeDriveClient:
        return FakeDriveClient(files={"folder-9": items, "_all": items})

    def item(self, name: str, mime: str, data: bytes) -> dict:
        return {"id": f"file-{name}", "name": name, "mimeType": mime,
                "size": str(len(data)), "modifiedTime": "2026-09-24T10:00:00Z", "_bytes": data}

    def test_imports_supported_media(self):
        image = self.item("hero.png", "image/png", png_bytes())
        video = self.item("clip.mp4", "video/mp4", mp4_bytes())
        result = drive_ops.ingest_media(self.client_with([image, video]), "folder-9")
        self.assertEqual(result["imported"], 2)
        self.assertEqual({item["mimeType"] for item in result["items"]}, {"image/png", "video/mp4"})
        self.assertEqual(len(list(self.media_dir.glob("*.png"))), 1)
        self.assertEqual(len(list(self.media_dir.glob("*.mp4"))), 1)

    def test_skips_google_native_unsupported_and_oversized_files(self):
        items = [
            self.item("notes", GOOGLE_SHEET_MIME, b"x"),
            self.item("doc.pdf", "application/pdf", b"%PDF-1.4"),
            {"id": "huge", "name": "huge.mp4", "mimeType": "video/mp4",
             "size": str(self.settings.max_video_bytes + 1), "modifiedTime": "2026-09-24T10:00:00Z", "_bytes": b""},
        ]
        result = drive_ops.ingest_media(self.client_with(items), "folder-9")
        self.assertEqual(result["imported"], 0)
        reasons = " ".join(item["reason"] for item in result["skippedItems"])
        self.assertIn("Google-native", reasons)
        self.assertIn("Unsupported media type", reasons)
        self.assertIn("Exceeds the configured limit", reasons)
        self.assertEqual(list(self.media_dir.glob("*")), [])

    def test_identical_media_is_not_stored_twice(self):
        first = self.item("hero.png", "image/png", png_bytes())
        self.assertEqual(drive_ops.ingest_media(self.client_with([first]), "folder-9")["imported"], 1)
        # A second run over the same folder must be a no-op, not a duplicate.
        again = drive_ops.ingest_media(self.client_with([first]), "folder-9")
        self.assertEqual(again["imported"], 0)
        self.assertIn("already exists", again["skippedItems"][0]["reason"])
        with self.engine.connect() as connection:
            count = connection.execute(select(content_assets.c.id)).all()
        self.assertEqual(len(count), 1, "the duplicate asset row must be rolled back")
        self.assertEqual(len(list(self.media_dir.glob("*.png"))), 1)

    def test_distinct_images_are_both_kept(self):
        items = [self.item("one.png", "image/png", png_bytes(colour=(1, 2, 3))),
                 self.item("two.png", "image/png", png_bytes(colour=(200, 100, 50)))]
        result = drive_ops.ingest_media(self.client_with(items), "folder-9")
        self.assertEqual(result["imported"], 2)
        self.assertEqual(len(list(self.media_dir.glob("*.png"))), 2)

    def test_dry_run_downloads_nothing(self):
        image = self.item("hero.png", "image/png", png_bytes())
        result = drive_ops.ingest_media(self.client_with([image]), "folder-9", dry_run=True)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(list(self.media_dir.glob("*")), [])


class DatabaseBackupTests(DriveOpsTestCase):
    def make_database(self, path: Path, with_tables: bool = True) -> Path:
        with sqlite3.connect(path) as connection:
            if with_tables:
                for table in sorted(drive_ops.REQUIRED_TABLES):
                    connection.execute(f'CREATE TABLE "{table}" (id TEXT)')
            connection.execute("CREATE TABLE filler (id TEXT)")
        return path

    def test_snapshot_produces_a_readable_copy(self):
        source = self.make_database(self.root / "live.sqlite")
        with patch.object(drive_ops, "sqlite_path", return_value=source):
            details = drive_ops.snapshot_database(self.root / "copy.sqlite")
        self.assertEqual(details["backend"], "sqlite")
        self.assertGreater(details["bytes"], 0)
        with sqlite3.connect(self.root / "copy.sqlite") as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("content_ideas", tables)

    def test_snapshot_reports_a_missing_database(self):
        with patch.object(drive_ops, "sqlite_path", return_value=self.root / "absent.sqlite"):
            with self.assertRaises(DriveError) as caught:
                drive_ops.snapshot_database(self.root / "copy.sqlite")
        self.assertIn("nothing to back up", str(caught.exception))

    def test_inspect_rejects_a_non_sqlite_file(self):
        candidate = self.root / "not-a-db.sqlite"
        candidate.write_bytes(b"definitely not a database")
        with self.assertRaises(DriveError) as caught:
            drive_ops._inspect_sqlite(candidate)
        self.assertIn("not a SQLite database", str(caught.exception))

    def test_inspect_rejects_a_database_missing_expected_tables(self):
        candidate = self.make_database(self.root / "partial.sqlite", with_tables=False)
        with self.assertRaises(DriveError) as caught:
            drive_ops._inspect_sqlite(candidate)
        self.assertIn("missing expected tables", str(caught.exception))

    def test_inspect_accepts_a_complete_database(self):
        candidate = self.make_database(self.root / "good.sqlite")
        details = drive_ops._inspect_sqlite(candidate)
        self.assertEqual(details["integrity"], "ok")
        self.assertIn("content_ideas", details["tables"])

    def test_verify_downloads_and_reports_integrity(self):
        good = self.make_database(self.root / "verify.sqlite").read_bytes()
        client = FakeDriveClient(files={"_all": [{"id": "backup-1", "name": "debelu.sqlite",
                                                  "size": str(len(good)), "mimeType": "application/vnd.sqlite3",
                                                  "_bytes": good}]})
        result = drive_ops.verify_database_backup(client, "backup-1")
        self.assertEqual(result["file"], "debelu.sqlite")
        self.assertEqual(result["integrity"], "ok")

    def test_restore_requires_explicit_confirmation(self):
        client = FakeDriveClient()
        with self.assertRaises(DriveError) as caught:
            drive_ops.restore_database(client, "backup-1")
        self.assertIn("confirm=True", str(caught.exception))

    def test_restore_replaces_the_database_and_keeps_a_safety_copy(self):
        live = self.make_database(self.root / "restore-target.sqlite")
        with sqlite3.connect(live) as connection:
            connection.execute("INSERT INTO filler VALUES ('old')")
        replacement = self.make_database(self.root / "replacement.sqlite").read_bytes()
        client = FakeDriveClient(files={"_all": [{"id": "backup-2", "name": "debelu.sqlite",
                                                  "mimeType": "application/vnd.sqlite3", "_bytes": replacement}]})
        with patch.object(drive_ops, "sqlite_path", return_value=live):
            result = drive_ops.restore_database(client, "backup-2", confirm=True)
        self.assertTrue(Path(result["target"]).is_file())
        safety = Path(result["safetyCopy"])
        self.assertTrue(safety.is_file(), "the previous database must be preserved")
        with sqlite3.connect(safety) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM filler").fetchone()[0], 1)

    def test_restore_refuses_an_invalid_file_and_leaves_the_database_alone(self):
        live = self.make_database(self.root / "keep.sqlite")
        original = live.read_bytes()
        client = FakeDriveClient(files={"_all": [{"id": "junk", "name": "junk.sqlite",
                                                  "mimeType": "application/octet-stream", "_bytes": b"garbage"}]})
        with patch.object(drive_ops, "sqlite_path", return_value=live):
            with self.assertRaises(DriveError):
                drive_ops.restore_database(client, "junk", confirm=True)
        self.assertEqual(live.read_bytes(), original, "a failed restore must not touch the live database")


class BackupOrchestrationTests(DriveOpsTestCase):
    def test_run_backup_uploads_database_media_status_and_prunes(self):
        source = self.root / "live.sqlite"
        with sqlite3.connect(source) as connection:
            for table in sorted(drive_ops.REQUIRED_TABLES):
                connection.execute(f'CREATE TABLE "{table}" (id TEXT)')
        (self.media_dir / "abc123.png").write_bytes(png_bytes())
        client = FakeDriveClient()
        client.files["folder-1"] = [  # database folder, oldest first by modifiedTime
            {"id": f"old-{index}", "name": f"old-{index}.sqlite", "modifiedTime": f"2026-09-0{index}T00:00:00Z"}
            for index in (1, 2, 3)
        ]
        with patch.object(drive_ops, "sqlite_path", return_value=source):
            summary = drive_ops.run_backup(client, prune=True)
        names = [upload["name"] for upload in client.uploads]
        self.assertTrue(any(name.endswith(".sqlite") for name in names))
        self.assertTrue(any(name.endswith(".zip") for name in names))
        self.assertTrue(any(name.endswith(".json") for name in names))
        self.assertEqual(summary["media"]["assets"], 1)
        # Retention is 2, so only the oldest of the three seeded backups is removed.
        self.assertEqual(client.deleted, ["old-1"])
        self.assertIn("drive_backup_completed", self.audit_actions())

    def test_media_archive_contains_the_media_and_skips_staged_uploads(self):
        (self.media_dir / "aaa.png").write_bytes(png_bytes())
        (self.media_dir / ".upload-pending.tmp").write_bytes(b"partial")
        client = FakeDriveClient()
        drive_ops.backup_media(client)
        archive = next(upload for upload in client.uploads if upload["name"].endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(archive["data"])) as bundle:
            self.assertEqual(bundle.namelist(), ["aaa.png"])

    def test_media_backup_reports_when_there_is_nothing_to_archive(self):
        result = drive_ops.backup_media(FakeDriveClient())
        self.assertEqual(result["assets"], 0)
        self.assertIn("No media files", result["skipped"])

    def test_status_manifest_reports_counts_without_secrets(self):
        client = FakeDriveClient()
        result = drive_ops.write_status_manifest(client)
        manifest = result["manifest"]
        self.assertIn("counts", manifest)
        self.assertFalse(manifest["publishingEnabled"])
        serialized = json.dumps(manifest).lower()
        for forbidden in ("password", "secret", "token", "private_key", "api_key"):
            self.assertNotIn(forbidden, serialized, f"the status manifest must not expose '{forbidden}'")


class ReportExportTests(DriveOpsTestCase):
    def test_exports_both_csv_files_with_formula_escaping(self):
        now = "2026-09-22T09:00:00+00:00"
        idea_id = "11111111-1111-1111-1111-111111111111"
        job_id = "22222222-2222-2222-2222-222222222222"
        with self.engine.begin() as connection:
            connection.execute(insert(content_ideas).values(
                id=idea_id, topic="=cmd|' /C calc'!A0", category="CLOUD", audience="Teams",
                status="PUBLISHED", created_at=now, updated_at=now,
            ))
            connection.execute(insert(content_variants).values(
                idea_id=idea_id, platform="threads", format="", body="", caption="", updated_at=now,
            ))
            # The weekly report is built from publish jobs, so a job is required for
            # the topic (and therefore the escaping) to appear in the CSV.
            connection.execute(insert(publish_jobs).values(
                id=job_id, idempotency_key="weekly-report-fixture", idea_id=idea_id,
                account_id="account-fixture", platform="threads", scheduled_for=now,
                status="PUBLISHED", content_hash="0" * 64, snapshot={},
                provider_url="https://example.test/post/1", created_at=now, updated_at=now,
                published_at=now,
            ))
        client = FakeDriveClient()
        result = drive_ops.export_reports(client, week="2026-09-21")
        names = [upload["name"] for upload in client.uploads]
        self.assertTrue(any(name.startswith("debelu-weekly-") for name in names))
        self.assertTrue(any(name.startswith("debelu-totals-") for name in names))
        weekly = next(upload for upload in client.uploads if upload["name"].startswith("debelu-weekly-"))
        self.assertIn("'=cmd", weekly["data"].decode("utf-8"), "spreadsheet formulas must be neutralised")
        self.assertEqual(result["weekStart"], "2026-09-21")


class PruneTests(DriveOpsTestCase):
    def test_prune_keeps_the_newest_and_deletes_the_rest(self):
        client = FakeDriveClient(files={"_all": [], "folder-1": [
            {"id": "a", "name": "a.sqlite", "modifiedTime": "2026-09-01T00:00:00Z"},
            {"id": "b", "name": "b.sqlite", "modifiedTime": "2026-09-03T00:00:00Z"},
            {"id": "c", "name": "c.sqlite", "modifiedTime": "2026-09-02T00:00:00Z"},
        ]})
        result = drive_ops.prune_backups(client, "database", keep=2)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["removed"], ["a.sqlite"])
        self.assertEqual(client.deleted, ["a"])

    def test_prune_is_a_no_op_below_retention(self):
        client = FakeDriveClient(files={"_all": [], "folder-1": [
            {"id": "a", "name": "a.sqlite", "modifiedTime": "2026-09-01T00:00:00Z"}]})
        result = drive_ops.prune_backups(client, "database", keep=5)
        self.assertEqual(result["removed"], [])
        self.assertEqual(client.deleted, [])


class CliTests(DriveOpsTestCase):
    """The CLI is the operator-facing surface, so its wiring is covered here."""

    def run_cli(self, argv: list[str], client: FakeDriveClient | None = None) -> tuple[int, str, str]:
        from backend import drive_cli
        out, err = io.StringIO(), io.StringIO()
        with patch.object(drive_cli, "build_client", return_value=client or FakeDriveClient()):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = drive_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_status_reports_configuration_without_credentials(self):
        code, out, _ = self.run_cli(["status"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["backupRetention"], 2)
        self.assertIsNone(payload["hint"])

    def test_folders_creates_and_lists_the_layout(self):
        client = FakeDriveClient()
        code, out, _ = self.run_cli(["folders"], client)
        self.assertEqual(code, 0)
        self.assertEqual(set(json.loads(out)["layout"]), {"root", "database", "media", "imports", "reports", "status"})

    def test_backup_prints_a_json_summary(self):
        source = self.root / "cli-live.sqlite"
        with sqlite3.connect(source) as connection:
            for table in sorted(drive_ops.REQUIRED_TABLES):
                connection.execute(f'CREATE TABLE "{table}" (id TEXT)')
        client = FakeDriveClient()
        with patch.object(drive_ops, "sqlite_path", return_value=source):
            code, out, _ = self.run_cli(["backup"], client)
        self.assertEqual(code, 0)
        self.assertIn("database", json.loads(out))

    def test_restore_without_confirm_is_refused_with_a_helpful_message(self):
        code, _, err = self.run_cli(["restore", "backup-1"])
        self.assertEqual(code, 1)
        self.assertIn("confirm=True", err)

    def test_drive_errors_become_exit_code_one(self):
        code, _, err = self.run_cli(["verify", "missing-file"])
        self.assertEqual(code, 1)
        self.assertIn("unknown file", err)

    def test_import_ideas_dry_run_leaves_the_workspace_empty(self):
        client = FakeDriveClient(
            files={"_all": [{"id": "sheet-1", "name": "Ideas", "mimeType": GOOGLE_SHEET_MIME}]},
            texts={"sheet-1": "topic\nFrom the CLI\n"},
        )
        code, out, _ = self.run_cli(["import-ideas", "sheet-1", "--dry-run"], client)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["dryRun"])
        self.assertEqual(self.ideas(), [])


if __name__ == "__main__":
    unittest.main()
