"""Command-line interface for the Google Drive side-channel features.

Run with ``python -m backend.drive_cli <command>``. Every command prints a JSON
summary so it can be captured by cron, a CI job or an operator's terminal.

    python -m backend.drive_cli folders
    python -m backend.drive_cli backup
    python -m backend.drive_cli list-backups
    python -m backend.drive_cli verify  <file-id>
    python -m backend.drive_cli restore <file-id> --confirm
    python -m backend.drive_cli import-ideas  <file-id> [--dry-run]
    python -m backend.drive_cli ingest-media  <folder-id> [--dry-run]
    python -m backend.drive_cli export-reports [--week 2026-09-21]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from backend import drive_ops
from backend.config import settings
from backend.gdrive import DriveError, build_client


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _client():
    return build_client()


def command_folders(_args: argparse.Namespace) -> dict:
    """Resolve (and create) the folder layout, so operators can confirm where things land."""
    client = _client()
    layout = {}
    for label, path in (
        ("root", ""),
        ("database", drive_ops.DATABASE_FOLDER),
        ("media", drive_ops.MEDIA_FOLDER),
        ("imports", drive_ops.IMPORT_FOLDER),
        ("reports", drive_ops.REPORT_FOLDER),
        ("status", drive_ops.STATUS_FOLDER),
    ):
        layout[label] = {"path": path or settings.gdrive_root_folder_name, "id": client.ensure_path(path)}
    return {"layout": layout, "shareWith": settings.gdrive_share_with or None}


def command_backup(args: argparse.Namespace) -> dict:
    return drive_ops.run_backup(_client(), prune=not args.no_prune)


def command_list_backups(_args: argparse.Namespace) -> dict:
    client = _client()
    folders = {}
    for path in (drive_ops.DATABASE_FOLDER, drive_ops.MEDIA_FOLDER):
        folder_id = client.ensure_path(path)
        files = client.list_files(folder_id)
        files.sort(key=lambda item: item.get("modifiedTime") or "", reverse=True)
        folders[path] = [
            {"name": item.get("name"), "id": item.get("id"), "size": item.get("size"),
             "modifiedTime": item.get("modifiedTime")}
            for item in files
        ]
    return {"retention": settings.gdrive_backup_retention, "folders": folders}


def command_verify(args: argparse.Namespace) -> dict:
    return drive_ops.verify_database_backup(_client(), args.file_id)


def command_restore(args: argparse.Namespace) -> dict:
    return drive_ops.restore_database(_client(), args.file_id, confirm=args.confirm)


def command_import_ideas(args: argparse.Namespace) -> dict:
    return drive_ops.import_ideas(_client(), args.file_id, dry_run=args.dry_run)


def command_ingest_media(args: argparse.Namespace) -> dict:
    return drive_ops.ingest_media(_client(), args.folder_id, dry_run=args.dry_run, limit=args.limit)


def command_export_reports(args: argparse.Namespace) -> dict:
    return drive_ops.export_reports(_client(), week=args.week, days=args.days)


def command_status(_args: argparse.Namespace) -> dict:
    """Report configuration without performing any writes."""
    configured = bool(settings.gdrive_enabled and settings.gdrive_service_account_json)
    return {
        "enabled": settings.gdrive_enabled,
        "credentialsConfigured": bool(settings.gdrive_service_account_json),
        "rootFolderName": settings.gdrive_root_folder_name,
        "rootFolderId": settings.gdrive_root_folder_id or None,
        "shareWith": settings.gdrive_share_with or None,
        "backupRetention": settings.gdrive_backup_retention,
        "canRead": settings.gdrive_enabled and configured and bool(settings.gdrive_scopes),
        "databaseBackend": drive_ops.database_url_parts().get_backend_name(),
        "mediaDirectory": str(settings.media_dir),
        "hint": "Drive features are disabled until GDRIVE_ENABLED=true." if not settings.gdrive_enabled else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend.drive_cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show Drive configuration without writing anything.")
    subparsers.add_parser("folders", help="Create and print the Drive folder layout.")

    backup = subparsers.add_parser("backup", help="Back up the database, media and a status manifest.")
    backup.add_argument("--no-prune", action="store_true", help="Keep all backups regardless of retention.")

    subparsers.add_parser("list-backups", help="List existing backups in Drive.")

    verify = subparsers.add_parser("verify", help="Download a backup and check its integrity.")
    verify.add_argument("file_id")

    restore = subparsers.add_parser("restore", help="Replace the live SQLite database with a backup.")
    restore.add_argument("file_id")
    restore.add_argument("--confirm", action="store_true", help="Required: the live database is overwritten.")

    ideas = subparsers.add_parser("import-ideas", help="Create ideas from a Sheet or CSV in Drive.")
    ideas.add_argument("file_id")
    ideas.add_argument("--dry-run", action="store_true")

    media = subparsers.add_parser("ingest-media", help="Import media from a Drive folder.")
    media.add_argument("folder_id")
    media.add_argument("--dry-run", action="store_true")
    media.add_argument("--limit", type=int, default=50)

    reports = subparsers.add_parser("export-reports", help="Upload weekly and lifetime CSV reports.")
    reports.add_argument("--week", default=None, help="Monday of the week, YYYY-MM-DD.")
    reports.add_argument("--days", type=int, default=30)

    return parser


COMMANDS = {
    "status": command_status,
    "folders": command_folders,
    "backup": command_backup,
    "list-backups": command_list_backups,
    "verify": command_verify,
    "restore": command_restore,
    "import-ideas": command_import_ideas,
    "ingest-media": command_ingest_media,
    "export-reports": command_export_reports,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _print(COMMANDS[args.command](args))
    except DriveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
