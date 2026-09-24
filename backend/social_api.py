from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, insert, select, update

from backend.analytics import query_analytics, weekly_report
from backend.config import settings
from backend.crypto import verify_public_asset_signature
from backend.database import (
    audit_events,
    content_assets,
    content_ideas,
    content_variants,
    engine,
    publish_jobs,
    social_accounts,
    variant_assets,
    worker_heartbeats,
)
from backend.media import delete_asset, get_asset, list_assets, public_asset_path, serialize_asset, storage_path, store_upload
from backend.publisher import cancel_publish_job, list_publish_jobs, retry_publish_job
from backend.social_accounts import (
    account_creator_info,
    connect_start,
    disconnect_account,
    list_accounts,
    oauth_callback,
)
from backend.social_providers import PlatformError

logger = logging.getLogger("debelu.social-api")
router = APIRouter()


class AssetAttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assetIds: list[str] = Field(default_factory=list, max_length=20)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(connection, action: str, idea_id: str | None, details: dict) -> None:
    connection.execute(insert(audit_events).values(
        id=str(uuid.uuid4()), idea_id=idea_id, actor="owner", action=action,
        details=json.dumps(details, ensure_ascii=False, separators=(",", ":"))[:4000], created_at=_now(),
    ))


def _platform_list() -> list[dict]:
    return [{
        "platform": platform,
        "configured": settings.provider_configured(platform),
        "connectable": settings.provider_configured(platform) and settings.social_publishing_enabled,
        "accountReviewRequired": platform in {"instagram", "threads"},
        "tiktokAudited": settings.tiktok_direct_post_audited if platform == "tiktok" else None,
    } for platform in ("instagram", "threads", "tiktok")]


@router.get("/api/social/status")
def social_status():
    with engine.connect() as connection:
        worker = connection.execute(select(worker_heartbeats).where(worker_heartbeats.c.component.like("publisher:%"))
                                    .order_by(worker_heartbeats.c.last_seen_at.desc()).limit(1)).mappings().first()
    worker_seen = None
    worker_healthy = False
    if worker:
        worker_seen = worker["last_seen_at"]
        try:
            worker_healthy = datetime.fromisoformat(worker_seen.replace("Z", "+00:00")) >= datetime.now(timezone.utc) - timedelta(seconds=90)
        except ValueError:
            worker_healthy = False
    return {
        "publishingEnabled": settings.social_publishing_enabled,
        "publicMediaReady": bool(settings.public_base_url),
        "worker": {"healthy": worker_healthy, "lastSeenAt": worker_seen, "status": worker["status"] if worker else "not_seen"},
        "providers": _platform_list(),
        "accounts": list_accounts(),
    }


@router.get("/api/social/accounts")
def accounts():
    return {"items": list_accounts()}


@router.post("/api/social/{platform}/connect")
def start_social_connection(platform: str, request: Request):
    cookie = request.cookies.get("debelu_session")
    url = connect_start(platform, cookie)
    return {"authorizationUrl": url}


@router.get("/api/social/{platform}/callback", include_in_schema=False)
def finish_social_connection(platform: str, request: Request, code: str | None = None,
                             state: str | None = None, error: str | None = None,
                             error_reason: str | None = None):
    if error:
        # Do not echo arbitrary provider-supplied error strings into a redirect or logs.
        logger.info("Social authorization cancelled for %s (%s)", platform, (error_reason or "denied")[:40])
        return RedirectResponse("/?social_result=cancelled", status_code=303)
    try:
        oauth_callback(platform, code or "", state or "", request.cookies.get("debelu_session"))
    except HTTPException as exc:
        logger.warning("Social authorization failed for %s with HTTP %s", platform, exc.status_code)
        return RedirectResponse("/?social_result=failed", status_code=303)
    except Exception:
        logger.exception("Social authorization callback failed for %s", platform)
        return RedirectResponse("/?social_result=failed", status_code=303)
    return RedirectResponse("/?social_result=connected", status_code=303)


@router.delete("/api/social/accounts/{account_id}")
def remove_social_account(account_id: str):
    return disconnect_account(account_id)


@router.post("/api/social/accounts/{account_id}/creator-info")
def get_creator_info(account_id: str):
    return account_creator_info(account_id)


@router.get("/api/assets")
def assets():
    return {"items": list_assets()}


@router.post("/api/assets", status_code=201)
def upload_asset(file: Annotated[UploadFile, File(...) ]):
    return store_upload(file)


@router.get("/api/assets/{asset_id}/content")
def authenticated_asset_content(asset_id: str):
    row = get_asset(asset_id)
    if not row:
        raise HTTPException(status_code=404, detail="Media asset not found.")
    return FileResponse(storage_path(row), media_type=row["mime_type"], headers={"Cache-Control": "private, max-age=300"})


@router.delete("/api/assets/{asset_id}", status_code=204)
def remove_asset(asset_id: str):
    delete_asset(asset_id)
    return Response(status_code=204)


@router.get("/public/media/{asset_id}", include_in_schema=False)
def public_media(asset_id: str, token: str = Query(min_length=1, max_length=200)):
    path, mime_type = public_asset_path(asset_id, token)
    return FileResponse(path, media_type=mime_type, headers={
        "Cache-Control": "public, max-age=300",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    })


@router.put("/api/ideas/{idea_id}/assets/{platform}")
def set_variant_assets(idea_id: str, platform: str, payload: AssetAttachmentInput):
    if platform not in {"instagram", "threads", "tiktok"}:
        raise HTTPException(status_code=422, detail="Unknown social platform.")
    if len(set(payload.assetIds)) != len(payload.assetIds):
        raise HTTPException(status_code=422, detail="A media asset can only be attached once to a platform draft.")
    if any(len(asset_id) > 36 for asset_id in payload.assetIds):
        raise HTTPException(status_code=422, detail="Invalid media asset ID.")
    now = _now()
    with engine.begin() as connection:
        idea = connection.execute(select(content_ideas).where(content_ideas.c.id == idea_id).with_for_update()).mappings().first()
        if not idea:
            raise HTTPException(status_code=404, detail="Content idea not found.")
        if idea["status"] == "PUBLISHED" or connection.execute(select(publish_jobs.c.id).where(
            publish_jobs.c.idea_id == idea_id, publish_jobs.c.status == "PUBLISHED",
        ).limit(1)).first():
            raise HTTPException(status_code=409, detail="A platform has already published this content. Create a new idea for a follow-up post.")
        if connection.execute(select(publish_jobs.c.id).where(
            publish_jobs.c.idea_id == idea_id,
            publish_jobs.c.status.in_(["PROCESSING", "REMOTE_PROCESSING", "UNKNOWN"]),
        ).limit(1)).first():
            raise HTTPException(status_code=409, detail="A platform post is processing or has an unknown outcome. Verify its status before changing media.")
        variant_exists = connection.execute(select(content_variants.c.idea_id).where(
            content_variants.c.idea_id == idea_id, content_variants.c.platform == platform,
        )).first()
        if not variant_exists:
            raise HTTPException(status_code=404, detail="Platform draft not found.")
        if payload.assetIds:
            existing_assets = set(connection.execute(select(content_assets.c.id).where(content_assets.c.id.in_(payload.assetIds))).scalars().all())
            missing = set(payload.assetIds) - existing_assets
            if missing:
                raise HTTPException(status_code=422, detail="One or more selected media assets do not exist.")
        previous = connection.execute(select(variant_assets.c.asset_id).where(
            variant_assets.c.idea_id == idea_id, variant_assets.c.platform == platform,
        ).order_by(variant_assets.c.position)).scalars().all()
        if list(previous) != payload.assetIds:
            connection.execute(delete(variant_assets).where(variant_assets.c.idea_id == idea_id, variant_assets.c.platform == platform))
            for position, asset_id in enumerate(payload.assetIds):
                connection.execute(insert(variant_assets).values(idea_id=idea_id, platform=platform, asset_id=asset_id, position=position))
            prior_status = idea["status"]
            values = {"updated_at": now}
            if prior_status in {"APPROVED", "SCHEDULED", "FAILED"}:
                values.update(status="READY_FOR_REVIEW", scheduled_for=None, approved_at=None, approved_hash=None)
                connection.execute(update(publish_jobs).where(
                    publish_jobs.c.idea_id == idea_id,
                    publish_jobs.c.status.in_(["SCHEDULED", "RETRY"]),
                ).values(status="CANCELLED", next_attempt_at=None, last_error_code="content_changed", last_error="Attached media changed after approval.", updated_at=now))
            connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(**values))
            _audit(connection, "media_attached", idea_id, {"platform": platform, "assetIds": payload.assetIds, "previousStatus": prior_status})
        rows = connection.execute(select(content_assets).join(
            variant_assets, content_assets.c.id == variant_assets.c.asset_id,
        ).where(variant_assets.c.idea_id == idea_id, variant_assets.c.platform == platform).order_by(variant_assets.c.position)).mappings().all()
    return {"assetIds": payload.assetIds, "assets": [serialize_asset(row) for row in rows], "updatedAt": now}


@router.get("/api/publishing/jobs")
def publishing_jobs(limit: int = Query(default=200, ge=1, le=500)):
    return {"items": list_publish_jobs(limit)}


@router.post("/api/publishing/jobs/{job_id}/cancel")
def cancel_publishing_job(job_id: str):
    return cancel_publish_job(job_id)


@router.post("/api/publishing/jobs/{job_id}/retry")
def retry_publishing_job(job_id: str):
    return retry_publish_job(job_id)


@router.get("/api/analytics")
def analytics(days: int = Query(default=30, ge=1, le=365), platform: str | None = None):
    return query_analytics(days, platform)


@router.get("/api/reports/weekly")
def weekly(week: str | None = None):
    return weekly_report(week)


@router.get("/api/reports/weekly.csv")
def weekly_csv(week: str | None = None):
    report = weekly_report(week)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["platform", "topic", "status", "published_at", "url"])
    for row in report["published"]:
        cells = [row["platform"], row["topic"], row["status"], row["publishedAt"], row["url"] or ""]
        writer.writerow([("'" + cell if isinstance(cell, str) and cell.startswith(("=", "+", "-", "@", "\t", "\r")) else cell) for cell in cells])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=debelu-weekly-report-{report['weekStart']}.csv",
        "Cache-Control": "private, no-store",
    })
