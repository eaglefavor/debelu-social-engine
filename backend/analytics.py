from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import insert, select, update

from backend.database import analytics_snapshots, audit_events, content_ideas, publish_jobs, social_accounts, engine
from backend.social_accounts import get_access_token
from backend.social_providers import PlatformError, get_provider


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or now_utc()).isoformat(timespec="seconds")


def _metric_period_end(raw: str, now: datetime) -> str:
    if not raw:
        return now.date().isoformat()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return now.date().isoformat()


def collect_job_metrics(job_id: str) -> int:
    now = now_utc()
    claimed_until = stamp(now + timedelta(minutes=30))
    with engine.begin() as connection:
        job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().first()
        if not job or job["status"] != "PUBLISHED" or not job["provider_post_id"]:
            return 0
        claimed = connection.execute(update(publish_jobs).where(
            publish_jobs.c.id == job_id,
            publish_jobs.c.status == "PUBLISHED",
            publish_jobs.c.provider_post_id != "",
            (publish_jobs.c.next_metrics_at.is_(None)) | (publish_jobs.c.next_metrics_at <= stamp(now)),
        ).values(next_metrics_at=claimed_until, updated_at=stamp(now))).rowcount
        if claimed != 1:
            return 0
        account = connection.execute(select(social_accounts).where(social_accounts.c.id == job["account_id"])).mappings().first()
    if not account or account["status"] != "CONNECTED":
        return 0
    try:
        access_token = get_access_token(job["account_id"])
        metrics = get_provider(job["platform"]).collect_metrics(access_token, account["external_account_id"], job["provider_post_id"])
    except PlatformError as exc:
        next_attempt = stamp(now + timedelta(hours=6 if exc.retryable else 24))
        with engine.begin() as connection:
            connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
                next_metrics_at=next_attempt, metrics_error=f"{exc.code}: {exc.message}"[:500], updated_at=stamp(now),
            ))
        return 0

    collected_at = stamp(now)
    recorded = 0
    with engine.begin() as connection:
        for metric in metrics:
            if not math.isfinite(float(metric.value)) or metric.value < 0:
                continue
            period_end = _metric_period_end(metric.period_end, now)
            key = hashlib.sha256(f"{job_id}:{metric.name}:{metric.period}:{period_end}".encode()).hexdigest()
            existing = connection.execute(select(analytics_snapshots.c.id).where(analytics_snapshots.c.observation_key == key)).first()
            if existing:
                connection.execute(update(analytics_snapshots).where(analytics_snapshots.c.observation_key == key).values(
                    value=float(metric.value), collected_at=collected_at,
                ))
            else:
                connection.execute(insert(analytics_snapshots).values(
                    id=str(uuid.uuid4()), observation_key=key, publish_job_id=job_id,
                    account_id=job["account_id"], platform=job["platform"], metric=metric.name[:100],
                    value=float(metric.value), period=metric.period[:40], period_end=period_end,
                    collected_at=collected_at,
                ))
            recorded += 1
        connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
            next_metrics_at=stamp(now + timedelta(hours=24)), metrics_error="", updated_at=collected_at,
        ))
        if recorded:
            connection.execute(insert(audit_events).values(
                id=str(uuid.uuid4()), idea_id=job["idea_id"], actor="system", action="analytics_collected",
                details=json.dumps({"platform": job["platform"], "count": recorded}, separators=(",", ":")), created_at=collected_at,
            ))
    return recorded


def collect_due_metrics(limit: int = 100) -> dict[str, int]:
    if limit < 1 or limit > 500:
        raise ValueError("Metric collection limit must be between 1 and 500.")
    now = stamp()
    with engine.connect() as connection:
        ids = connection.execute(select(publish_jobs.c.id).where(
            publish_jobs.c.status == "PUBLISHED",
            publish_jobs.c.provider_post_id != "",
            (publish_jobs.c.next_metrics_at.is_(None)) | (publish_jobs.c.next_metrics_at <= now),
        ).order_by(publish_jobs.c.published_at).limit(limit)).scalars().all()
    collected = failed = 0
    for job_id in ids:
        try:
            collected += collect_job_metrics(job_id)
        except Exception:
            failed += 1
    return {"jobsChecked": len(ids), "metricsCollected": collected, "failures": failed}


def query_analytics(days: int = 30, platform: str | None = None) -> dict[str, Any]:
    if not 1 <= days <= 365:
        raise HTTPException(status_code=422, detail="days must be between 1 and 365.")
    if platform and platform not in {"instagram", "threads", "tiktok"}:
        raise HTTPException(status_code=422, detail="Unknown social platform.")
    cutoff = (now_utc() - timedelta(days=days)).date().isoformat()
    with engine.connect() as connection:
        query = select(analytics_snapshots, publish_jobs.c.idea_id, publish_jobs.c.provider_post_id,
                       publish_jobs.c.provider_url, content_ideas.c.topic).join(
            publish_jobs, analytics_snapshots.c.publish_job_id == publish_jobs.c.id
        ).join(content_ideas, publish_jobs.c.idea_id == content_ideas.c.id).where(analytics_snapshots.c.period_end >= cutoff)
        if platform:
            query = query.where(analytics_snapshots.c.platform == platform)
        rows = connection.execute(query.order_by(analytics_snapshots.c.period_end.desc())).mappings().all()
    return {"days": days, "metrics": [{
        "platform": row["platform"], "metric": row["metric"], "value": row["value"],
        "period": row["period"], "periodEnd": row["period_end"], "collectedAt": row["collected_at"],
        "ideaId": row["idea_id"], "topic": row["topic"], "providerPostId": row["provider_post_id"],
        "providerUrl": row["provider_url"] or None,
    } for row in rows]}


def weekly_report(week_start: str | None = None) -> dict[str, Any]:
    today = now_utc().date()
    try:
        start = date.fromisoformat(week_start) if week_start else today - timedelta(days=today.weekday() + 7)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="week must be a Monday date in YYYY-MM-DD format.") from exc
    if start.weekday() != 0:
        raise HTTPException(status_code=422, detail="week must be a Monday date in YYYY-MM-DD format.")
    end = start + timedelta(days=7)
    with engine.connect() as connection:
        jobs = connection.execute(select(
            publish_jobs.c.id, publish_jobs.c.platform, publish_jobs.c.status,
            publish_jobs.c.published_at, publish_jobs.c.provider_url, content_ideas.c.topic,
        ).join(content_ideas, publish_jobs.c.idea_id == content_ideas.c.id)
          .where(publish_jobs.c.published_at >= stamp(datetime.combine(start, datetime.min.time(), timezone.utc)),
                 publish_jobs.c.published_at < stamp(datetime.combine(end, datetime.min.time(), timezone.utc)))
          .order_by(publish_jobs.c.published_at)).mappings().all()
        job_ids = [row["id"] for row in jobs]
        metrics = connection.execute(select(analytics_snapshots).where(
            analytics_snapshots.c.publish_job_id.in_(job_ids) if job_ids else analytics_snapshots.c.id == "__none__",
            analytics_snapshots.c.period_end >= start.isoformat(),
            analytics_snapshots.c.period_end < end.isoformat(),
        ).order_by(analytics_snapshots.c.period_end.desc())).mappings().all()
        outstanding = connection.execute(select(publish_jobs.c.platform, publish_jobs.c.status).where(
            publish_jobs.c.scheduled_for >= stamp(datetime.combine(start, datetime.min.time(), timezone.utc)),
            publish_jobs.c.scheduled_for < stamp(datetime.combine(end, datetime.min.time(), timezone.utc)),
        )).all()
    totals: dict[str, dict[str, float]] = {}
    latest_by_post_metric: dict[tuple[str, str, str], Any] = {}
    for row in metrics:
        key = (row["publish_job_id"], row["platform"], row["metric"])
        latest_by_post_metric.setdefault(key, row)  # rows are ordered newest first
    for (_job_id, platform, metric_name), row in latest_by_post_metric.items():
        platform_totals = totals.setdefault(platform, {})
        platform_totals[metric_name] = platform_totals.get(metric_name, 0.0) + float(row["value"])
    published = [{"platform": row["platform"], "status": row["status"], "publishedAt": row["published_at"],
                  "topic": row["topic"], "url": row["provider_url"] or None} for row in jobs]
    recommendation = "No reliable performance recommendation yet; publish consistently and collect at least several weeks of measured results."
    if len(published) >= 5 and totals:
        recommendation = "Use the platform-level measurements as directional evidence only. Compare like-for-like formats and review post context before changing the plan."
    return {
        "weekStart": start.isoformat(), "weekEndExclusive": end.isoformat(),
        "publishedCount": len(published), "scheduledOrPendingCount": sum(1 for row in outstanding if row.status in {"SCHEDULED", "PROCESSING", "REMOTE_PROCESSING", "RETRY"}),
        "published": published, "metricsByPlatform": totals,
        "recommendation": recommendation,
        "measurementNote": "Counts reflect only provider metrics collected by this app. Metrics and definitions differ by platform and are not directly comparable.",
    }
