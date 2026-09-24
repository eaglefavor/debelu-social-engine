from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
import warnings
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import delete, insert, select

from backend.config import settings
from backend.database import content_assets, engine, variant_assets
from backend.crypto import make_public_asset_url, verify_public_asset_signature
from backend.social_providers import PlatformError

Image.MAX_IMAGE_PIXELS = 50_000_000
SUPPORTED_IMAGES = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}
MP4_BRAND_OFFSET = 4
STORAGE_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp|mp4)$")


def _safe_filename(value: str) -> str:
    name = Path(value or "upload").name
    cleaned = "".join(character for character in name if character.isprintable() and character not in "\\/\x00")
    cleaned = cleaned.strip().lstrip(".")[:240]
    return cleaned or "upload"


def _probe_video(path: Path) -> float | None:
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, timeout=12, text=True,
        )
        duration = float(json.loads(result.stdout).get("format", {}).get("duration", 0))
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="The video could not be decoded by ffprobe; export a valid MP4 and try again.") from exc
    if duration <= 0 or duration > 3600:
        raise HTTPException(status_code=422, detail="Video duration must be greater than 0 and at most 60 minutes.")
    return round(duration, 3)


def _write_upload(upload: UploadFile, destination: Path, maximum: int) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    total = 0
    head = bytearray()
    with destination.open("wb") as output:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise HTTPException(status_code=413, detail=f"File exceeds the configured {maximum // (1024 * 1024)} MiB limit.")
            if len(head) < 32:
                head.extend(chunk[:32 - len(head)])
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if total == 0:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")
    return total, digest.hexdigest(), bytes(head)


def _normalize_image(path: Path) -> tuple[str, str, int, int, int, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                if image_format not in SUPPORTED_IMAGES:
                    raise HTTPException(status_code=415, detail="Upload a JPEG, PNG or WebP image.")
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                if width < 64 or height < 64 or width > 12000 or height > 12000 or width * height > Image.MAX_IMAGE_PIXELS:
                    raise HTTPException(status_code=422, detail="Image dimensions are outside the supported range.")
                mime_type, extension = SUPPORTED_IMAGES[image_format]
                output = io.BytesIO()
                if image_format == "JPEG":
                    if image.mode not in {"RGB", "L"}:
                        background = Image.new("RGB", image.size, "white")
                        if "A" in image.getbands():
                            background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
                        else:
                            background.paste(image.convert("RGB"))
                        image = background
                    image.save(output, format="JPEG", quality=92, optimize=True, progressive=True)
                elif image_format == "PNG":
                    image.save(output, format="PNG", optimize=True)
                else:
                    image.save(output, format="WEBP", quality=92, method=5)
                cleaned = output.getvalue()
                path.write_bytes(cleaned)
                os.chmod(path, 0o600)
                return mime_type, extension, width, height, len(cleaned), hashlib.sha256(cleaned).hexdigest()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=415, detail="The image file is corrupt, unsupported or unsafe to decode.") from exc


def finalize_staged_media(staged: Path, original_name: str, size: int, source_digest: str, header: bytes) -> dict:
    """Validate a staged file, move it into place and record it as a content asset.

    Shared by browser uploads and Drive intake so both enforce identical type,
    size and normalisation rules.
    """
    final_path: Path | None = None
    try:
        if header[MP4_BRAND_OFFSET:MP4_BRAND_OFFSET + 4] == b"ftyp":
            if size > settings.max_video_bytes:
                raise HTTPException(status_code=413, detail="Video exceeds the configured upload limit.")
            duration = _probe_video(staged)
            extension = ".mp4"
            mime_type = "video/mp4"
            width = height = None
            digest = source_digest
            final_size = size
        else:
            if size > settings.max_image_bytes:
                raise HTTPException(status_code=413, detail="Image exceeds the configured upload limit.")
            mime_type, extension, width, height, final_size, digest = _normalize_image(staged)
            duration = None
        asset_id = str(uuid.uuid4())
        storage_key = f"{asset_id.replace('-', '')}{extension}"
        final_path = settings.media_dir / storage_key
        os.replace(staged, final_path)
        os.chmod(final_path, 0o600)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = {
            "id": asset_id,
            "original_name": _safe_filename(original_name or "upload"),
            "mime_type": mime_type,
            "storage_key": storage_key,
            "size_bytes": final_size,
            "sha256": digest,
            "width": width,
            "height": height,
            "duration_seconds": duration,
            "created_at": now,
        }
        with engine.begin() as connection:
            connection.execute(insert(content_assets).values(**row))
        return serialize_asset(row)
    except Exception:
        if final_path and final_path.exists():
            final_path.unlink(missing_ok=True)
        raise


def store_upload(upload: UploadFile) -> dict:
    settings.media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = settings.media_dir / f".upload-{uuid.uuid4().hex}.tmp"
    try:
        # Initial limit accommodates either image or video; type-specific limits are enforced after sniffing.
        size, source_digest, header = _write_upload(upload, staged, max(settings.max_image_bytes, settings.max_video_bytes))
        return finalize_staged_media(staged, upload.filename or "upload", size, source_digest, header)
    finally:
        staged.unlink(missing_ok=True)
        upload.file.close()


def store_media_bytes(data: bytes, original_name: str) -> dict:
    """Store already-fetched bytes (for example a file downloaded from Drive)."""
    settings.media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = settings.media_dir / f".upload-{uuid.uuid4().hex}.tmp"
    try:
        with open(staged, "wb") as handle:
            handle.write(data)
        header = data[:32]
        source_digest = hashlib.sha256(data).hexdigest()
        return finalize_staged_media(staged, original_name, len(data), source_digest, header)
    finally:
        staged.unlink(missing_ok=True)


def serialize_asset(row) -> dict:
    return {
        "id": row["id"],
        "name": row["original_name"],
        "mimeType": row["mime_type"],
        "sizeBytes": row["size_bytes"],
        "sha256": row["sha256"],
        "width": row["width"],
        "height": row["height"],
        "durationSeconds": row["duration_seconds"],
        "createdAt": row["created_at"],
    }


def list_assets() -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(select(content_assets).order_by(content_assets.c.created_at.desc()).limit(500)).mappings().all()
    return [serialize_asset(row) for row in rows]


def get_asset(asset_id: str):
    with engine.connect() as connection:
        return connection.execute(select(content_assets).where(content_assets.c.id == asset_id)).mappings().first()


def storage_path(row) -> Path:
    storage_key = row["storage_key"]
    if not STORAGE_KEY_PATTERN.fullmatch(storage_key):
        raise HTTPException(status_code=500, detail="Stored asset key is invalid.")
    path = (settings.media_dir / storage_key).resolve()
    if path.parent != settings.media_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Media file is unavailable.")
    return path


def public_asset_path(asset_id: str, signed_token: str) -> tuple[Path, str]:
    if not verify_public_asset_signature(asset_id, signed_token):
        raise HTTPException(status_code=403, detail="The media URL is invalid or has expired.")
    row = get_asset(asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Media file not found.")
    return storage_path(row), row["mime_type"]


def delete_asset(asset_id: str) -> None:
    row = get_asset(asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Media asset not found.")
    with engine.begin() as connection:
        used = connection.execute(select(variant_assets.c.asset_id).where(variant_assets.c.asset_id == asset_id).limit(1)).first()
        if used:
            raise HTTPException(status_code=409, detail="Detach this media from every platform draft before deleting it.")
        connection.execute(delete(content_assets).where(content_assets.c.id == asset_id))
    storage_path(row).unlink(missing_ok=True)


def asset_for_provider(asset_id: str) -> dict:
    row = get_asset(asset_id)
    if not row:
        raise PlatformError("unknown", "asset_missing", "A scheduled media asset is no longer available.")
    if not settings.public_base_url:
        raise PlatformError("unknown", "public_url_missing", "Set APP_PUBLIC_URL to an HTTPS address reachable by social platforms before publishing media.")
    try:
        url = make_public_asset_url(asset_id)
    except RuntimeError as exc:
        raise PlatformError("unknown", "public_url_missing", str(exc)) from exc
    return {**serialize_asset(row), "public_url": url, "mime_type": row["mime_type"], "storage_path": storage_path(row)}
