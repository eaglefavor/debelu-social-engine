from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, or_, select, update

from backend.config import settings
from backend.crypto import make_public_asset_url
from backend.database import (
    audit_events,
    content_assets,
    content_ideas,
    publish_jobs,
    social_accounts,
    variant_assets,
    engine,
)
from backend.media import storage_path
from backend.social_accounts import get_access_token
from backend.social_providers import PlatformError, PublishReceipt, get_provider
from backend.workflow import current_fingerprint

ACTIVE_JOB_STATUSES = {"SCHEDULED", "PROCESSING", "REMOTE_PROCESSING", "RETRY"}
TERMINAL_FAILURES = {"FAILED", "UNKNOWN", "NEEDS_ATTENTION"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def worker_id() -> str:
    return f"publisher-{uuid.uuid4().hex[:16]}"


def _audit(connection, action: str, job, details: dict[str, Any]) -> None:
    connection.execute(insert(audit_events).values(
        id=str(uuid.uuid4()), idea_id=job["idea_id"], actor="system", action=action,
        details=json.dumps(details, ensure_ascii=False, separators=(",", ":"))[:4000], created_at=stamp(),
    ))


def recover_expired_leases(limit: int = 100) -> int:
    """Never blindly resubmit after a worker died mid-request; reconcile when we have a provider ID."""
    now = stamp()
    recovered = 0
    with engine.begin() as connection:
        rows = connection.execute(select(publish_jobs).where(
            publish_jobs.c.status == "PROCESSING",
            publish_jobs.c.lease_until.is_not(None),
            publish_jobs.c.lease_until <= now,
        ).order_by(publish_jobs.c.lease_until).limit(limit)).mappings().all()
        for row in rows:
            next_status = "REMOTE_PROCESSING" if row["provider_post_id"] else "UNKNOWN"
            message = "Worker lease expired; provider status will be checked." if next_status == "REMOTE_PROCESSING" else "Worker stopped during a publish request. External result is unknown; verify the platform before retrying."
            changed = connection.execute(update(publish_jobs).where(
                publish_jobs.c.id == row["id"], publish_jobs.c.status == "PROCESSING",
                publish_jobs.c.lease_until <= now,
            ).values(
                status=next_status,
                next_attempt_at=now if next_status == "REMOTE_PROCESSING" else None,
                lease_owner="", lease_until=None,
                last_error_code="worker_lease_expired", last_error=message,
                updated_at=now,
            )).rowcount
            if changed:
                _audit(connection, "publish_job_recovered", row, {"status": next_status})
                recovered += 1
    return recovered


def claim_due_jobs(worker: str, limit: int = 10) -> list[str]:
    now = stamp()
    due = or_(
        (publish_jobs.c.status == "SCHEDULED") & (publish_jobs.c.scheduled_for <= now),
        (publish_jobs.c.status == "RETRY") & (publish_jobs.c.next_attempt_at <= now),
        (publish_jobs.c.status == "REMOTE_PROCESSING") & (publish_jobs.c.next_attempt_at <= now),
    )
    claimed: list[str] = []
    lease_until = stamp(utc_now() + timedelta(seconds=settings.worker_lease_seconds))
    with engine.begin() as connection:
        rows = connection.execute(select(publish_jobs.c.id, publish_jobs.c.status).where(due)
                                  .order_by(publish_jobs.c.scheduled_for, publish_jobs.c.created_at).limit(limit)).all()
        for row in rows:
            changed = connection.execute(update(publish_jobs).where(
                publish_jobs.c.id == row.id,
                publish_jobs.c.status == row.status,
                due,
            ).values(status="PROCESSING", lease_owner=worker, lease_until=lease_until, updated_at=now)).rowcount
            if changed == 1:
                claimed.append(row.id)
    return claimed


def _load_job(job_id: str):
    with engine.connect() as connection:
        job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().first()
        if not job:
            return None, None, None
        account = connection.execute(select(social_accounts).where(social_accounts.c.id == job["account_id"])).mappings().first()
        idea = connection.execute(select(content_ideas).where(content_ideas.c.id == job["idea_id"])).mappings().first()
        return job, account, idea


def _assets_for_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for asset_id in snapshot.get("assetIds", []):
        with engine.connect() as connection:
            asset = connection.execute(select(content_assets).where(content_assets.c.id == asset_id)).mappings().first()
        if not asset:
            raise PlatformError(snapshot.get("platform", "unknown"), "asset_missing", "A scheduled media asset is no longer available.")
        storage_path(asset)  # Confirm the file still exists before handing its URL to a provider.
        try:
            public_url = make_public_asset_url(asset_id)
        except RuntimeError as exc:
            raise PlatformError(snapshot.get("platform", "unknown"), "public_url_missing", str(exc)) from exc
        prepared.append({
            "id": asset_id,
            "mime_type": asset["mime_type"],
            "duration_seconds": asset["duration_seconds"],
            "size_bytes": asset["size_bytes"],
            "width": asset["width"],
            "height": asset["height"],
            "public_url": public_url,
        })
    return prepared


def _record_failure(job_id: str, worker: str, error: PlatformError, attempt: int, is_poll: bool) -> None:
    now_dt = utc_now()
    now = stamp(now_dt)
    safe_message = error.message[:500]
    with engine.begin() as connection:
        job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().first()
        if not job or job["status"] != "PROCESSING" or job["lease_owner"] != worker:
            return
        remote_deadline = parse_stamp(job["remote_deadline_at"])
        poll_exhausted = is_poll and (job["poll_count"] >= 120 or (remote_deadline is not None and remote_deadline <= now_dt))
        if poll_exhausted:
            status = "NEEDS_ATTENTION"
            next_at = None
            error_code = "provider_poll_timeout"
            safe_message = "The provider status could not be confirmed within 30 minutes. Check the social account before taking further action."
        elif error.ambiguous and not is_poll:
            status = "UNKNOWN"
            next_at = None
            error_code = error.code[:100]
        elif error.reauth_required:
            status = "NEEDS_ATTENTION"
            next_at = None
            error_code = error.code[:100]
        elif error.retryable and attempt < settings.worker_max_attempts:
            status = "RETRY"
            backoff = error.retry_after or min(900, 5 * (2 ** max(0, attempt - 1)))
            backoff += random.randint(0, min(7, max(0, backoff // 5)))
            next_at = stamp(now_dt + timedelta(seconds=backoff))
            error_code = error.code[:100]
        else:
            status = "NEEDS_ATTENTION"
            next_at = None
            error_code = error.code[:100]
        connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
            status=status,
            attempt_count=attempt,
            poll_count=job["poll_count"] + (1 if is_poll else 0),
            next_attempt_at=next_at,
            lease_owner="", lease_until=None,
            last_error_code=error_code, last_error=safe_message,
            updated_at=now,
        ))
        if error.reauth_required:
            connection.execute(update(social_accounts).where(social_accounts.c.id == job["account_id"]).values(
                status="REAUTH_REQUIRED", last_error=safe_message[:240], updated_at=now,
            ))
        _audit(connection, "publish_job_failed" if status != "UNKNOWN" else "publish_job_outcome_unknown", job,
               {"platform": job["platform"], "status": status, "code": error.code, "attempt": attempt})
        _update_idea_rollup(connection, job["idea_id"], now)


def _record_remote_processing(job_id: str, worker: str, receipt: PublishReceipt, *, attempt: int, is_poll: bool) -> None:
    now_dt = utc_now()
    now = stamp(now_dt)
    remote_deadline = stamp(now_dt + timedelta(minutes=30))
    with engine.begin() as connection:
        job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().first()
        if not job or job["status"] != "PROCESSING" or job["lease_owner"] != worker:
            return
        deadline = job["remote_deadline_at"] or remote_deadline
        if parse_stamp(deadline) <= now_dt or job["poll_count"] >= 120:
            status = "NEEDS_ATTENTION"
            next_at = None
            error = "The platform did not finish processing within 30 minutes. Check the account before retrying."
        else:
            status = "REMOTE_PROCESSING"
            next_at = stamp(now_dt + timedelta(seconds=min(60, 10 + job["poll_count"] * 5)))
            error = receipt.status_detail[:500]
        connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
            status=status,
            provider_post_id=receipt.external_id,
            provider_url=receipt.permalink or job["provider_url"],
            attempt_count=attempt,
            poll_count=job["poll_count"] + (1 if is_poll else 0),
            remote_deadline_at=deadline,
            next_attempt_at=next_at,
            lease_owner="", lease_until=None,
            last_error_code="provider_processing" if status == "REMOTE_PROCESSING" else "provider_timeout",
            last_error=error,
            updated_at=now,
        ))
        _audit(connection, "provider_processing" if status == "REMOTE_PROCESSING" else "provider_processing_timeout", job,
               {"platform": job["platform"], "providerId": receipt.external_id, "status": status})
        _update_idea_rollup(connection, job["idea_id"], now)


def _record_published(job_id: str, worker: str, receipt: PublishReceipt, attempt: int, is_poll: bool) -> None:
    now = stamp()
    with engine.begin() as connection:
        job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().first()
        if not job or job["status"] != "PROCESSING" or job["lease_owner"] != worker:
            return
        connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
            status="PUBLISHED", provider_post_id=receipt.external_id,
            provider_url=receipt.permalink or job["provider_url"],
            published_at=now, attempt_count=attempt,
            poll_count=job["poll_count"] + (1 if is_poll else 0),
            next_attempt_at=None, next_metrics_at=stamp(utc_now() + timedelta(minutes=5)),
            lease_owner="", lease_until=None,
            last_error_code="", last_error="", updated_at=now,
        ))
        _audit(connection, "social_post_published", job, {
            "platform": job["platform"], "accountId": job["account_id"],
            "providerPostId": receipt.external_id, "url": receipt.permalink,
        })
        _update_idea_rollup(connection, job["idea_id"], now)


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _update_idea_rollup(connection, idea_id: str, now: str) -> None:
    idea = connection.execute(select(content_ideas).where(content_ideas.c.id == idea_id)).mappings().first()
    if not idea or idea["status"] == "PUBLISHED":
        return
    jobs = connection.execute(select(publish_jobs.c.status, publish_jobs.c.provider_url, publish_jobs.c.published_at)
                              .where(publish_jobs.c.idea_id == idea_id)).all()
    if not jobs:
        return
    statuses = [row.status for row in jobs]
    if any(status in ACTIVE_JOB_STATUSES for status in statuses):
        connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(status="SCHEDULED", updated_at=now))
    elif any(status in TERMINAL_FAILURES for status in statuses):
        connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(status="FAILED", updated_at=now))
    elif any(status == "PUBLISHED" for status in statuses):
        published = [row.published_at for row in jobs if row.published_at]
        urls = [row.provider_url for row in jobs if row.provider_url]
        connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(
            status="PUBLISHED", published_at=max(published) if published else now,
            published_url=urls[0] if urls else "", updated_at=now,
        ))
    else:
        # Every queued target was cancelled before any social post was submitted.
        connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(
            status="APPROVED", scheduled_for=None, updated_at=now,
        ))


def _process_one(job_id: str, worker: str) -> None:
    job, account, idea = _load_job(job_id)
    if not job or job["status"] != "PROCESSING" or job["lease_owner"] != worker:
        return
    if not account or account["status"] != "CONNECTED":
        _record_failure(job_id, worker, PlatformError(job["platform"], "account_disconnected", "Reconnect the social account before this post can publish."), job["attempt_count"] + 1, bool(job["provider_post_id"]))
        return
    if not idea or idea["status"] not in {"SCHEDULED", "APPROVED"}:
        _record_failure(job_id, worker, PlatformError(job["platform"], "content_not_approved", "This content is no longer approved for publishing."), job["attempt_count"] + 1, bool(job["provider_post_id"]))
        return
    with engine.connect() as connection:
        live_fingerprint = current_fingerprint(connection, idea)
    if not idea["approved_hash"] or idea["approved_hash"] != job["content_hash"] or live_fingerprint != job["content_hash"]:
        _record_failure(job_id, worker, PlatformError(job["platform"], "approval_changed", "Approved content changed after this schedule was created. Review and schedule again."), job["attempt_count"] + 1, bool(job["provider_post_id"]))
        return
    snapshot = dict(job["snapshot"])
    snapshot["platform"] = job["platform"]
    is_poll = bool(job["provider_post_id"] and job["status"] == "PROCESSING")
    attempt = int(job["attempt_count"]) + (0 if is_poll else 1)
    try:
        token = get_access_token(job["account_id"])
        provider = get_provider(job["platform"])
        if is_poll:
            receipt = provider.poll_publish(token, account["external_account_id"], job["provider_post_id"])
        else:
            assets = _assets_for_snapshot(snapshot)
            receipt = provider.publish(token, account["external_account_id"], snapshot, assets)
        if receipt.complete:
            _record_published(job_id, worker, receipt, attempt, is_poll)
        else:
            _record_remote_processing(job_id, worker, receipt, attempt=attempt, is_poll=is_poll)
    except PlatformError as exc:
        if is_poll and exc.ambiguous:
            exc.ambiguous = False  # Status checks are safe to repeat; the post itself is not re-submitted.
            exc.retryable = True
        _record_failure(job_id, worker, exc, attempt, is_poll)
    except Exception:
        # Unknown exceptions are never retried automatically because a provider may have accepted a POST.
        error = PlatformError(job["platform"], "internal_error", "The publishing worker encountered an internal error; verify platform status before retrying.", retryable=is_poll, ambiguous=not is_poll)
        _record_failure(job_id, worker, error, attempt, is_poll)


def run_publishing_once(worker: str | None = None, limit: int = 10) -> dict[str, int]:
    if not settings.social_publishing_enabled:
        return {"claimed": 0, "completed": 0, "recovered": 0, "disabled": 1}
    worker = worker or worker_id()
    recovered = recover_expired_leases()
    job_ids = claim_due_jobs(worker, limit)
    completed_before = 0
    for job_id in job_ids:
        _process_one(job_id, worker)
        with engine.connect() as connection:
            row = connection.execute(select(publish_jobs.c.status).where(publish_jobs.c.id == job_id)).first()
        if row and row[0] == "PUBLISHED":
            completed_before += 1
    return {"claimed": len(job_ids), "completed": completed_before, "recovered": recovered, "disabled": 0}


def public_job(row) -> dict:
    snapshot = row.get("snapshot") or {}
    stored_tiktok = snapshot.get("tiktok") or {}
    tiktok_options = ({
        "privacyLevel": stored_tiktok.get("privacyLevel", ""),
        "allowComment": bool(stored_tiktok.get("allowComment")),
        "allowDuet": bool(stored_tiktok.get("allowDuet")),
        "allowStitch": bool(stored_tiktok.get("allowStitch")),
        "ownBrand": bool(stored_tiktok.get("ownBrand")),
        "brandedContent": bool(stored_tiktok.get("brandedContent")),
        "isAigc": bool(stored_tiktok.get("isAigc")),
        "consentGiven": bool(stored_tiktok.get("consentAt")),
    } if row["platform"] == "tiktok" else None)
    return {
        "id": row["id"],
        "ideaId": row["idea_id"],
        "accountId": row["account_id"],
        "platform": row["platform"],
        "tiktok": tiktok_options,
        "scheduledFor": row["scheduled_for"],
        "status": row["status"],
        "providerPostId": row["provider_post_id"] or None,
        "providerUrl": row["provider_url"] or None,
        "attemptCount": row["attempt_count"],
        "lastErrorCode": row["last_error_code"] or None,
        "lastError": row["last_error"] or None,
        "publishedAt": row["published_at"],
        "createdAt": row["created_at"],
    }


def list_publish_jobs(limit: int = 200) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(select(publish_jobs).order_by(publish_jobs.c.scheduled_for.desc()).limit(limit)).mappings().all()
    return [public_job(row) for row in rows]


def cancel_publish_job(job_id: str) -> dict:
    now = stamp()
    with engine.begin() as connection:
        row = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().first()
        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Publishing job not found.")
        if row["status"] not in {"SCHEDULED", "RETRY"}:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="A post already being processed or submitted cannot be cancelled safely.")
        connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
            status="CANCELLED", next_attempt_at=None, last_error_code="cancelled_by_owner",
            last_error="Cancelled by the workspace owner.", updated_at=now,
        ))
        _audit(connection, "publish_job_cancelled", row, {"platform": row["platform"]})
        _update_idea_rollup(connection, row["idea_id"], now)
        updated = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
    return public_job(updated)


def retry_publish_job(job_id: str) -> dict:
    if not settings.social_publishing_enabled:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Social publishing is disabled on this server.")
    now = stamp()
    with engine.begin() as connection:
        job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id).with_for_update()).mappings().first()
        if not job:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Publishing job not found.")
        if job["status"] not in {"FAILED", "NEEDS_ATTENTION"} or job["provider_post_id"]:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="Only a definitive, unsubmitted failure can be retried. Verify provider status for any ambiguous outcome.")
        account = connection.execute(select(social_accounts).where(social_accounts.c.id == job["account_id"])).mappings().first()
        if not account or account["status"] != "CONNECTED":
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="Reconnect the social account before retrying.")
        idea = connection.execute(select(content_ideas).where(content_ideas.c.id == job["idea_id"]).with_for_update()).mappings().first()
        if not idea or not idea["approved_hash"] or idea["approved_hash"] != job["content_hash"] or current_fingerprint(connection, idea) != job["content_hash"]:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="The approval snapshot no longer matches this post. Review and approve the latest version first.")
        connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
            status="RETRY", attempt_count=0, next_attempt_at=now,
            last_error_code="", last_error="", lease_owner="", lease_until=None, updated_at=now,
        ))
        connection.execute(update(content_ideas).where(content_ideas.c.id == job["idea_id"]).values(status="SCHEDULED", updated_at=now))
        _audit(connection, "publish_job_manually_retried", job, {"platform": job["platform"]})
        updated = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
    return public_job(updated)


def schedule_jobs(connection, idea: dict, approved_hash: str, scheduled_for: str,
                  requests: list[dict[str, Any]], now: str) -> list[dict]:
    """Insert jobs in the same transaction as the approval-checked calendar update."""
    result: list[dict] = []
    for target in requests:
        platform = target["platform"]
        account_id = target["accountId"]
        snapshot = {
            "topic": idea["topic"],
            "category": idea["category"],
            "audience": idea["audience"],
            "platform": platform,
            "variant": idea["variants"][platform],
            "assetIds": idea["variants"][platform].get("assetIds", []),
            "tiktok": target.get("tiktok", {}) if platform == "tiktok" else {},
        }
        key_source = f"{idea['id']}:{platform}:{account_id}:{approved_hash}:{scheduled_for}:{json.dumps(snapshot, sort_keys=True, separators=(',', ':'))}"
        idempotency_key = hashlib.sha256(key_source.encode()).hexdigest()
        existing = connection.execute(select(publish_jobs).where(publish_jobs.c.idempotency_key == idempotency_key)).mappings().first()
        if existing:
            if existing["status"] == "CANCELLED":
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == existing["id"]).values(
                    status="SCHEDULED", scheduled_for=scheduled_for, next_attempt_at=None,
                    provider_post_id="", provider_url="", attempt_count=0, poll_count=0,
                    remote_deadline_at=None, lease_owner="", lease_until=None,
                    last_error_code="", last_error="", updated_at=now,
                ))
                existing = connection.execute(select(publish_jobs).where(publish_jobs.c.id == existing["id"])).mappings().one()
            result.append(public_job(existing))
            continue
        job_id = str(uuid.uuid4())
        connection.execute(insert(publish_jobs).values(
            id=job_id,
            idempotency_key=idempotency_key,
            idea_id=idea["id"],
            account_id=account_id,
            platform=platform,
            scheduled_for=scheduled_for,
            status="SCHEDULED",
            content_hash=approved_hash,
            snapshot=snapshot,
            provider_post_id="",
            provider_url="",
            attempt_count=0,
            poll_count=0,
            next_attempt_at=scheduled_for,
            remote_deadline_at=None,
            lease_owner="",
            lease_until=None,
            last_error_code="",
            last_error="",
            created_at=now,
            updated_at=now,
            published_at=None,
        ))
        inserted = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
        result.append(public_job(inserted))
        _audit(connection, "publish_job_scheduled", inserted, {"platform": platform, "accountId": account_id, "scheduledFor": scheduled_for})
    return result
