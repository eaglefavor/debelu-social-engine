"""Google Drive side-channel operations for Debelu Social Engine.

Drive is **not** this application's backend and cannot be: it runs no code, has no
transactions and cannot serve the signed media URLs that Meta and TikTok fetch.
What it is genuinely good at is the work that happens *around* the request path:

* ``backup_database`` / ``backup_media`` / ``run_backup`` - durable off-host copies,
  closing the gap the README already flags (PostgreSQL **and** the media volume).
* ``restore_database`` / ``verify_database_backup`` - a backup you cannot restore or
  verify is not a backup.
* ``import_ideas`` - plan content in a Google Sheet, pull it into the workspace.
* ``ingest_media`` - bulk-load source images and video from a Drive folder.
* ``export_reports`` - measured weekly results as CSV for review.
* ``write_status_manifest`` - a small health/activity record for operators.

Everything is idempotent where it can be (idea topics and asset digests are
de-duplicated), every write is reported back as a summary dict, and nothing here
touches the network unless ``GDRIVE_ENABLED=true``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.engine import make_url

from backend.analytics import query_analytics, weekly_report
from backend.config import settings
from backend.database import (
    analytics_snapshots,
    audit_events,
    content_assets,
    content_ideas,
    content_variants,
    engine,
    publish_jobs,
    social_accounts,
    worker_heartbeats,
)
from backend.gdrive import GOOGLE_DOC_MIME, GOOGLE_SHEET_MIME, GOOGLE_SLIDE_MIME, DriveClient, DriveError, build_client
from backend.media import delete_asset, store_media_bytes

logger = logging.getLogger("debelu.drive")

DATABASE_FOLDER = "database"
MEDIA_FOLDER = "media"
IMPORT_FOLDER = "imports"
REPORT_FOLDER = "reports"
STATUS_FOLDER = "status"

PLATFORMS = ("instagram", "threads", "tiktok")

# Ideas created from a spreadsheet arrive without a guaranteed category; the AI
# generator falls back to CYBERSECURITY for the same reason, so imports match it.
DEFAULT_CATEGORY = "CYBERSECURITY"
CATEGORIES = {
    "CYBERSECURITY", "CLOUD", "APPLICATION SECURITY", "APPSEC", "DEVSECOPS",
    "SOFTWARE ENGINEERING", "AI SECURITY", "NETWORK SECURITY", "DATA SECURITY",
    "API SECURITY", "DEVELOPER SECURITY", "BUILD IN PUBLIC", "DEBELU VENTURES",
    "SECURITY MYTH", "SECURITY PRACTICE", "CLOUD SECURITY",
}

# Only real, byte-backed media can be attached to a draft. Google-native types
# (Docs/Sheets/Slides) are not downloadable media and are skipped by intake.
INGEST_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "video/mp4"}
SKIPPED_MIME_TYPES = {GOOGLE_DOC_MIME, GOOGLE_SHEET_MIME, GOOGLE_SLIDE_MIME}
SQLITE_MAGIC = b"SQLite format 3\x00"
STAGED_PREFIX = ".upload-"

REQUIRED_TABLES = {
    "content_ideas", "content_variants", "content_assets", "variant_assets",
    "publish_jobs", "social_accounts", "analytics_snapshots", "worker_heartbeats",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or now_utc()).isoformat(timespec="seconds")


def slug(value: datetime | None = None) -> str:
    return (value or now_utc()).strftime("%Y%m%dT%H%M%SZ")


def _audit(connection, action: str, details: dict[str, Any] | None = None) -> None:
    connection.execute(insert(audit_events).values(
        id=str(uuid.uuid4()),
        idea_id=None,
        actor="owner",
        action=action,
        details=json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
        created_at=stamp(),
    ))


def _csv_cell(value: Any) -> Any:
    """Mirror the weekly CSV endpoint's spreadsheet-formula guard."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def database_url_parts():
    return make_url(settings.database_url)


def sqlite_path() -> Path | None:
    url = database_url_parts()
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database or ""
    if not database or database == ":memory:":
        return None
    path = Path(database).expanduser()
    return path if path.is_absolute() else (settings.project_root / path).resolve()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def snapshot_database(destination: Path) -> dict:
    """Write a consistent copy of the database to ``destination``.

    SQLite uses the online backup API, so this is safe against a running web
    process and a running publisher worker. PostgreSQL shells out to ``pg_dump``.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = database_url_parts()
    backend = url.get_backend_name()

    if backend == "sqlite":
        source = sqlite_path()
        if source is None or not source.is_file():
            raise DriveError(f"SQLite database not found at {source}; nothing to back up.")
        with sqlite3.connect(str(source)) as source_connection, sqlite3.connect(str(destination)) as target:
            source_connection.backup(target)
        return {"backend": backend, "source": source.name, "bytes": destination.stat().st_size}

    executable = shutil.which("pg_dump")
    if not executable:
        raise DriveError("pg_dump is not on PATH; install the PostgreSQL client tools to back up PostgreSQL.")
    # Credentials travel through the environment, never through argv.
    environment = {
        **os.environ,
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "postgres",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "postgres",
    }
    result = subprocess.run(
        [executable, "--format=custom", "--no-password", "--file", str(destination)],
        env=environment, capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        raise DriveError(f"pg_dump failed ({result.returncode}): {result.stderr.strip()[:300]}")
    return {"backend": backend, "source": url.database, "bytes": destination.stat().st_size}


def backup_database(client: DriveClient | None = None, now: datetime | None = None) -> dict:
    client = client or build_client()
    moment = now or now_utc()
    folder = client.ensure_path(DATABASE_FOLDER)
    name = f"debelu-{slug(moment)}.sqlite"
    with tempfile.TemporaryDirectory(prefix="debelu-backup-") as directory:
        destination = Path(directory) / name
        details = snapshot_database(destination)
        file_id = client.upload_bytes(name, destination.read_bytes(), folder, "application/vnd.sqlite3")
    result = {"file": name, "fileId": file_id, "folder": DATABASE_FOLDER, **details, "createdAt": stamp(moment)}
    logger.info("Backed up database to Drive as %s (%s bytes)", name, details["bytes"])
    return result


def backup_media(client: DriveClient | None = None, now: datetime | None = None) -> dict:
    client = client or build_client()
    moment = now or now_utc()
    folder = client.ensure_path(MEDIA_FOLDER)
    name = f"debelu-media-{slug(moment)}.zip"
    media_dir = settings.media_dir
    included = 0
    with tempfile.TemporaryDirectory(prefix="debelu-media-") as directory:
        archive = Path(directory) / name
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            if media_dir.is_dir():
                for path in sorted(media_dir.rglob("*")):
                    if not path.is_file() or path.name.startswith(STAGED_PREFIX):
                        continue
                    bundle.write(path, path.relative_to(media_dir).as_posix())
                    included += 1
        if included == 0:
            return {"file": None, "fileId": None, "folder": MEDIA_FOLDER, "assets": 0,
                    "bytes": 0, "createdAt": stamp(moment), "skipped": "No media files to archive."}
        file_id = client.upload_bytes(name, archive.read_bytes(), folder, "application/zip")
        size = archive.stat().st_size
    result = {"file": name, "fileId": file_id, "folder": MEDIA_FOLDER, "assets": included,
              "bytes": size, "createdAt": stamp(moment)}
    logger.info("Backed up %s media files to Drive as %s (%s bytes)", included, name, size)
    return result


def write_status_manifest(client: DriveClient | None = None, now: datetime | None = None) -> dict:
    """Record counts and flags an operator can read without database access.

    Deliberately excludes credentials, tokens and provider secrets: this file is
    meant to be safe to open from a browser or share with a collaborator.
    """
    client = client or build_client()
    moment = now or now_utc()
    folder = client.ensure_path(STATUS_FOLDER)
    with engine.connect() as connection:
        counts = {
            "ideas": connection.execute(select(func.count()).select_from(content_ideas)).scalar_one(),
            "assets": connection.execute(select(func.count()).select_from(content_assets)).scalar_one(),
            "publishJobs": connection.execute(select(func.count()).select_from(publish_jobs)).scalar_one(),
            "socialAccounts": connection.execute(select(func.count()).select_from(social_accounts)).scalar_one(),
            "analyticsSnapshots": connection.execute(select(func.count()).select_from(analytics_snapshots)).scalar_one(),
        }
        heartbeat = connection.execute(
            select(worker_heartbeats.c.component, worker_heartbeats.c.last_seen_at, worker_heartbeats.c.status)
            .order_by(worker_heartbeats.c.last_seen_at.desc()).limit(1)
        ).mappings().first()
    media_bytes = 0
    if settings.media_dir.is_dir():
        media_bytes = sum(path.stat().st_size for path in settings.media_dir.rglob("*") if path.is_file())
    manifest = {
        "generatedAt": stamp(moment),
        "timezone": settings.timezone,
        "databaseBackend": database_url_parts().get_backend_name(),
        "counts": counts,
        "mediaDirectoryBytes": media_bytes,
        "publishingEnabled": settings.social_publishing_enabled,
        "aiConfigured": bool(settings.ai_api_key),
        "worker": {
            "component": heartbeat["component"] if heartbeat else None,
            "lastSeenAt": heartbeat["last_seen_at"] if heartbeat else None,
            "status": heartbeat["status"] if heartbeat else "not_seen",
        },
    }
    name = f"status-{slug(moment)}.json"
    file_id = client.upload_bytes(name, json.dumps(manifest, indent=2).encode("utf-8"), folder, "application/json")
    return {"file": name, "fileId": file_id, "folder": STATUS_FOLDER, "manifest": manifest}


def prune_backups(client: DriveClient, folder_path: str, keep: int | None = None) -> dict:
    """Delete the oldest files in a backup folder, keeping the newest ``keep``."""
    retention = settings.gdrive_backup_retention if keep is None else keep
    folder = client.ensure_path(folder_path)
    files = [item for item in client.list_files(folder) if item.get("name")]
    files.sort(key=lambda item: item.get("modifiedTime") or "", reverse=True)
    removed = []
    for item in files[retention:]:
        client.delete_file(item["id"])
        removed.append(item["name"])
    return {"folder": folder_path, "kept": min(len(files), retention), "removed": removed}


def run_backup(client: DriveClient | None = None, now: datetime | None = None, prune: bool = True) -> dict:
    """One call an operator or cron can run: database, media, manifest, retention."""
    client = client or build_client()
    moment = now or now_utc()
    summary: dict[str, Any] = {
        "database": backup_database(client, moment),
        "media": backup_media(client, moment),
        "status": write_status_manifest(client, moment),
    }
    if prune:
        summary["pruned"] = {
            DATABASE_FOLDER: prune_backups(client, DATABASE_FOLDER),
            MEDIA_FOLDER: prune_backups(client, MEDIA_FOLDER),
        }
    with engine.begin() as connection:
        _audit(connection, "drive_backup_completed", {
            "databaseFile": summary["database"].get("file"),
            "mediaAssets": summary["media"].get("assets", 0),
        })
    return summary


# ---------------------------------------------------------------------------
# Restore and verification
# ---------------------------------------------------------------------------

def _inspect_sqlite(path: Path) -> dict:
    """Validate a candidate database before it is allowed anywhere near the live one."""
    header = path.read_bytes()[:len(SQLITE_MAGIC)]
    if header != SQLITE_MAGIC:
        raise DriveError("The selected file is not a SQLite database (bad header).")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise DriveError(f"SQLite integrity check failed: {integrity[0] if integrity else 'no result'}")
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    except sqlite3.DatabaseError as exc:
        raise DriveError(f"The database file could not be read: {exc}") from exc
    tables = {row[0] for row in rows}
    missing = sorted(REQUIRED_TABLES - tables)
    if missing:
        raise DriveError(f"Backup is missing expected tables: {', '.join(missing)}")
    return {"tables": sorted(tables), "integrity": "ok"}


def verify_database_backup(client: DriveClient, file_id: str) -> dict:
    """Download a backup and confirm it is a usable Debelu database."""
    with tempfile.TemporaryDirectory(prefix="debelu-verify-") as directory:
        candidate = Path(directory) / "candidate.sqlite"
        candidate.write_bytes(client.download_bytes(file_id))
        metadata = client.get_file(file_id)
        details = _inspect_sqlite(candidate)
    return {"file": metadata.get("name"), "fileId": file_id, "bytes": metadata.get("size"),
            "modifiedTime": metadata.get("modifiedTime"), **details}


def restore_database(client: DriveClient, file_id: str, confirm: bool = False,
                     now: datetime | None = None) -> dict:
    """Replace the live database with a downloaded backup.

    Requires ``confirm=True``, refuses files that fail header or integrity checks,
    and always writes a safety copy of the current database next to the target
    before swapping. Restoring PostgreSQL is intentionally unsupported: use
    ``pg_restore`` against a maintenance window instead.
    """
    if not confirm:
        raise DriveError("Refusing to restore without confirm=True; this overwrites the live database.")
    target = sqlite_path()
    if target is None:
        raise DriveError("restore_database supports SQLite only. Use pg_restore for PostgreSQL.")
    moment = now or now_utc()
    with tempfile.TemporaryDirectory(prefix="debelu-restore-") as directory:
        candidate = Path(directory) / "candidate.sqlite"
        candidate.write_bytes(client.download_bytes(file_id))
        inspected = _inspect_sqlite(candidate)
        safety_copy: Path | None = None
        if target.is_file():
            safety_copy = target.with_name(f"{target.stem}.pre-restore-{slug(moment)}{target.suffix}")
            shutil.copy2(target, safety_copy)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Replace atomically so a crash cannot leave a half-written database behind.
        os.replace(candidate, target)
    result = {"restoredFrom": file_id, "target": str(target), "tables": inspected["tables"],
              "safetyCopy": str(safety_copy) if safety_copy else None, "restoredAt": stamp(moment)}
    logger.warning("Restored database from Drive file %s; previous database saved to %s", file_id, safety_copy)
    return result


# ---------------------------------------------------------------------------
# Idea import
# ---------------------------------------------------------------------------

def parse_ideas_csv(text: str) -> list[dict]:
    """Parse ``topic,category,audience,instagram,threads,tiktok`` rows.

    ``topic`` is required; every other column is optional. Unknown categories fall
    back to CYBERSECURITY so one typo cannot reject an otherwise valid sheet.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise DriveError("The import file is empty.")
    normalized = {(name or "").strip().lower(): name for name in reader.fieldnames}
    if "topic" not in normalized:
        raise DriveError("The import file needs a 'topic' column header.")
    ideas: list[dict] = []
    for index, row in enumerate(reader, start=2):
        topic = (row.get(normalized["topic"]) or "").strip()
        if not topic:
            continue
        category = (row.get(normalized.get("category", ""), "") or "").strip().upper()
        audience = (row.get(normalized.get("audience", ""), "") or "").strip()
        variants = {}
        for platform in PLATFORMS:
            column = normalized.get(platform)
            body = (row.get(column, "") or "").strip() if column else ""
            if body:
                variants[platform] = {"format": "", "body": body, "caption": ""}
        ideas.append({
            "row": index,
            "topic": topic[:240],
            "category": category if category in CATEGORIES else DEFAULT_CATEGORY,
            "audience": audience[:240],
            "variants": variants,
        })
    return ideas


def import_ideas(client: DriveClient, file_id: str, dry_run: bool = False) -> dict:
    """Create workspace ideas from a Sheet, CSV or text file in Drive.

    Idempotent by topic: a topic already present in the workspace is skipped, so
    re-running an import after adding rows only creates the new ones.
    """
    metadata = client.get_file(file_id)
    text = client.read_text(file_id, metadata.get("mimeType"))
    parsed = parse_ideas_csv(text)
    if not parsed:
        return {"file": metadata.get("name"), "created": 0, "skipped": 0, "items": [], "dryRun": dry_run}
    now = stamp()
    created: list[dict] = []
    skipped: list[dict] = []
    with engine.begin() as connection:
        existing = {
            str(row[0]).strip().lower()
            for row in connection.execute(select(content_ideas.c.topic)).all()
        }
        for idea in parsed:
            if idea["topic"].lower() in existing:
                skipped.append({"row": idea["row"], "topic": idea["topic"], "reason": "Topic already exists."})
                continue
            if dry_run:
                created.append({"row": idea["row"], "topic": idea["topic"], "dryRun": True})
                continue
            idea_id = str(uuid.uuid4())
            connection.execute(insert(content_ideas).values(
                id=idea_id, topic=idea["topic"], category=idea["category"], audience=idea["audience"],
                status="IDEA", created_at=now, updated_at=now,
            ))
            for platform in sorted(PLATFORMS):
                variant = idea["variants"].get(platform)
                connection.execute(insert(content_variants).values(
                    idea_id=idea_id, platform=platform,
                    format=variant["format"] if variant else "",
                    body=variant["body"] if variant else "",
                    caption=variant["caption"] if variant else "",
                    updated_at=now,
                ))
            existing.add(idea["topic"].lower())
            created.append({"row": idea["row"], "topic": idea["topic"], "ideaId": idea_id})
        if not dry_run and created:
            _audit(connection, "drive_ideas_imported", {
                "file": metadata.get("name"), "created": len(created), "skipped": len(skipped),
            })
    return {"file": metadata.get("name"), "created": len(created), "skipped": len(skipped),
            "items": created, "skippedItems": skipped, "dryRun": dry_run}


# ---------------------------------------------------------------------------
# Media intake
# ---------------------------------------------------------------------------

def existing_asset_digests() -> set[str]:
    with engine.connect() as connection:
        return {row[0] for row in connection.execute(select(content_assets.c.sha256)).all()}


def ingest_media(client: DriveClient, folder_id: str, dry_run: bool = False, limit: int = 50) -> dict:
    """Download media from a Drive folder into the local asset library.

    Files are validated by the same code path as browser uploads and de-duplicated
    by SHA-256, so re-running against the same folder is safe: already-imported
    files are reported as skipped rather than stored twice.
    """
    candidates = client.list_files(folder_id)
    known = existing_asset_digests()
    imported: list[dict] = []
    skipped: list[dict] = []
    for item in candidates:
        if len(imported) >= limit:
            skipped.append({"name": "…", "reason": f"Per-run limit of {limit} files reached."})
            break
        name = item.get("name") or "unnamed"
        mime_type = item.get("mimeType") or ""
        if mime_type in SKIPPED_MIME_TYPES:
            skipped.append({"name": name, "reason": "Google-native file is not downloadable media."})
            continue
        if mime_type not in INGEST_MIME_TYPES:
            skipped.append({"name": name, "reason": f"Unsupported media type: {mime_type or 'unknown'}."})
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        ceiling = settings.max_video_bytes if mime_type.startswith("video/") else settings.max_image_bytes
        if size and size > ceiling:
            skipped.append({"name": name, "reason": f"Exceeds the configured limit ({size} bytes)."})
            continue
        if dry_run:
            imported.append({"name": name, "mimeType": mime_type, "dryRun": True})
            continue
        data = client.download_bytes(item["id"])
        # Deduplicate on the stored digest rather than the downloaded bytes. Images are
        # re-encoded on the way in (EXIF rotation, re-compression), so the stored digest
        # legitimately differs from the source file's - comparing source digests would
        # re-import every JPEG on each run. Storing first and rolling back an exact
        # duplicate keeps this precise for both images and video.
        asset = store_media_bytes(data, name)
        if asset["sha256"] in known:
            delete_asset(asset["id"])
            skipped.append({"name": name, "reason": "Identical media already exists in the library."})
            continue
        known.add(asset["sha256"])
        imported.append({"name": name, "assetId": asset["id"], "mimeType": asset["mimeType"],
                         "sizeBytes": asset["sizeBytes"], "durationSeconds": asset["durationSeconds"]})
    if not dry_run and imported:
        with engine.begin() as connection:
            _audit(connection, "drive_media_ingested", {"folder": folder_id, "imported": len(imported)})
    return {"folderId": folder_id, "imported": len(imported), "skipped": len(skipped),
            "items": imported, "skippedItems": skipped, "dryRun": dry_run}


# ---------------------------------------------------------------------------
# Report export
# ---------------------------------------------------------------------------

def export_reports(client: DriveClient, week: str | None = None, days: int = 30,
                   now: datetime | None = None) -> dict:
    """Publish the weekly report and lifetime analytics to Drive as CSV."""
    client = client or build_client()
    moment = now or now_utc()
    folder = client.ensure_path(REPORT_FOLDER)
    report = weekly_report(week)

    weekly = io.StringIO(newline="")
    writer = csv.writer(weekly)
    writer.writerow(["platform", "topic", "status", "published_at", "url"])
    for row in report["published"]:
        writer.writerow([_csv_cell(cell) for cell in
                         (row["platform"], row["topic"], row["status"], row["publishedAt"], row["url"] or "")])

    analytics = query_analytics(days)
    totals = io.StringIO(newline="")
    totals_writer = csv.writer(totals)
    totals_writer.writerow(["platform", "metric", "value"])
    for platform, metrics in sorted((analytics.get("totals") or {}).items()):
        for metric, value in sorted(metrics.items()):
            totals_writer.writerow([_csv_cell(cell) for cell in (platform, metric, value)])

    uploaded = []
    for suffix, buffer in (("weekly", weekly), ("totals", totals)):
        name = f"debelu-{suffix}-{report['weekStart'] if suffix == 'weekly' else slug(moment)}.csv"
        file_id = client.upload_bytes(name, buffer.getvalue().encode("utf-8"), folder, "text/csv")
        uploaded.append({"file": name, "fileId": file_id, "folder": REPORT_FOLDER})
    return {"weekStart": report["weekStart"], "uploads": uploaded, "createdAt": stamp(moment)}
