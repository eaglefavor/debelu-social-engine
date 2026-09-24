from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection

from backend.ai import AIProviderError, generate_week as generate_ai_week
from backend.config import PROJECT_ROOT, settings
from backend.database import (
    audit_events, brand_profiles, content_assets, content_ideas, content_variants,
    engine, init_database, publish_jobs, social_accounts, variant_assets,
)
from backend.analytics import query_analytics, weekly_report
from backend.media import list_assets, serialize_asset
from backend.publisher import list_publish_jobs, public_job, schedule_jobs
from backend.social_accounts import account_creator_info, is_platform_configured, list_accounts
from backend.social_providers import PlatformError
from backend.workflow import current_fingerprint
from backend.security import (
    clear_login_failures,
    login_is_limited,
    make_session_cookie,
    note_login_failure,
    valid_session_cookie,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("debelu.social-engine")

DEFAULT_BRAND: dict[str, str] = {
    "industry": "Cybersecurity, cloud, application security and software development",
    "voice": "Technical, professional, modern, confident, educational and human",
    "avoid": "Fearmongering, fake statistics, overpromising, technical misinformation, excessive corporate language and generic motivation",
    "visual": "Dark navy and black, electric blue, white typography, technical grids and minimal UI elements",
    "tagline": "SECURE · BUILD · SCALE",
}
PLATFORMS = {"instagram", "threads", "tiktok"}
CATEGORIES = {
    "CYBERSECURITY", "CLOUD", "APPLICATION SECURITY", "APPSEC", "DEVSECOPS",
    "SOFTWARE ENGINEERING", "AI SECURITY", "NETWORK SECURITY", "DATA SECURITY",
    "API SECURITY", "DEVELOPER SECURITY", "BUILD IN PUBLIC", "DEBELU VENTURES",
    "SECURITY MYTH", "SECURITY PRACTICE", "CLOUD SECURITY",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def local_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown APP_TIMEZONE: {settings.timezone}") from exc


def audit(connection: Connection, action: str, idea_id: str | None = None, details: dict[str, Any] | None = None) -> None:
    connection.execute(insert(audit_events).values(
        id=str(uuid.uuid4()),
        idea_id=idea_id,
        actor="owner",
        action=action,
        details=json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
        created_at=timestamp(),
    ))


def get_idea_row(connection: Connection, idea_id: str):
    return connection.execute(select(content_ideas).where(content_ideas.c.id == idea_id)).mappings().first()


def serialize_idea(connection: Connection, row) -> dict[str, Any]:
    variants = connection.execute(
        select(content_variants).where(content_variants.c.idea_id == row["id"])
    ).mappings().all()
    asset_rows = connection.execute(
        select(content_assets, variant_assets.c.platform, variant_assets.c.position)
        .join(variant_assets, content_assets.c.id == variant_assets.c.asset_id)
        .where(variant_assets.c.idea_id == row["id"])
        .order_by(variant_assets.c.platform, variant_assets.c.position)
    ).mappings().all()
    assets_by_platform: dict[str, list[dict[str, Any]]] = {platform: [] for platform in PLATFORMS}
    for asset in asset_rows:
        assets_by_platform.setdefault(asset["platform"], []).append(serialize_asset(asset))
    by_platform = {
        variant["platform"]: {
            "format": variant["format"],
            "body": variant["body"],
            "caption": variant["caption"],
            "updatedAt": variant["updated_at"],
            "assets": assets_by_platform.get(variant["platform"], []),
            "assetIds": [asset["id"] for asset in assets_by_platform.get(variant["platform"], [])],
        }
        for variant in variants
    }
    jobs = connection.execute(select(publish_jobs).where(publish_jobs.c.idea_id == row["id"])
                              .order_by(publish_jobs.c.created_at.desc())).mappings().all()
    return {
        "id": row["id"],
        "topic": row["topic"],
        "category": row["category"],
        "audience": row["audience"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "scheduledFor": row["scheduled_for"],
        "suggestedFor": row["suggested_for"],
        "planBatch": row["plan_batch"],
        "approvedAt": row["approved_at"],
        "publishedAt": row["published_at"],
        "publishedUrl": row["published_url"],
        "variants": by_platform,
        "publishingJobs": [public_job(job) for job in jobs],
    }


def fetch_idea(connection: Connection, idea_id: str) -> dict[str, Any]:
    row = get_idea_row(connection, idea_id)
    if not row:
        raise HTTPException(status_code=404, detail="Content idea not found.")
    return serialize_idea(connection, row)


def brand_profile(connection: Connection) -> dict[str, str]:
    row = connection.execute(select(brand_profiles).where(brand_profiles.c.id == "default")).mappings().first()
    if not row:
        return dict(DEFAULT_BRAND)
    return {key: row[key] for key in DEFAULT_BRAND}


def has_copy(connection: Connection, idea_id: str) -> bool:
    rows = connection.execute(
        select(content_variants.c.body).where(content_variants.c.idea_id == idea_id)
    ).all()
    return any((row[0] or "").strip() for row in rows)


def parse_scheduled_time(value: str) -> str:
    try:
        candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Provide a valid ISO date and time.") from exc
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=local_timezone())
    if candidate <= utc_now() + timedelta(minutes=1):
        raise HTTPException(status_code=422, detail="Choose a time at least one minute in the future.")
    return timestamp(candidate)


def validate_variant_result(raw: Any, platform: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise AIProviderError(f"The AI response was missing its {platform} version.")
    format_name = str(raw.get("format", "")).strip()[:100]
    body = str(raw.get("body", "")).strip()
    caption = str(raw.get("caption", "")).strip()
    if len(body) < 30 or len(body) > 9000 or len(caption) > 2500:
        raise AIProviderError(f"The AI response contained incomplete or oversized {platform} copy. Try again.")
    if not format_name:
        raise AIProviderError(f"The AI response was missing the format for {platform}.")
    return {"format": format_name, "body": body, "caption": caption}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    password: str = Field(min_length=1, max_length=256)


class VariantInput(StrictModel):
    format: str = Field(default="", max_length=100)
    body: str = Field(default="", max_length=9000)
    caption: str = Field(default="", max_length=2500)


class IdeaCreate(StrictModel):
    topic: str = Field(min_length=3, max_length=240)
    category: str = Field(default="CYBERSECURITY", min_length=2, max_length=80)
    audience: str = Field(default="", max_length=240)
    variants: dict[str, VariantInput] = Field(default_factory=dict)


class IdeaUpdate(StrictModel):
    topic: str | None = Field(default=None, min_length=3, max_length=240)
    category: str | None = Field(default=None, min_length=2, max_length=80)
    audience: str | None = Field(default=None, max_length=240)
    variants: dict[str, VariantInput] | None = None


class TikTokPublishOptions(StrictModel):
    privacyLevel: str = Field(min_length=3, max_length=40)
    allowComment: bool
    allowDuet: bool
    allowStitch: bool
    ownBrand: bool
    brandedContent: bool
    isAigc: bool
    consentGiven: bool


class PublishTarget(StrictModel):
    platform: str
    accountId: str = Field(min_length=36, max_length=36)
    tiktok: TikTokPublishOptions | None = None


class ScheduleRequest(StrictModel):
    scheduledFor: str = Field(min_length=10, max_length=80)
    targets: list[PublishTarget] = Field(default_factory=list, max_length=3)


class PublishNowRequest(StrictModel):
    targets: list[PublishTarget] = Field(min_length=1, max_length=3)


class BrandProfileInput(StrictModel):
    industry: str = Field(min_length=2, max_length=1500)
    voice: str = Field(min_length=2, max_length=1500)
    avoid: str = Field(min_length=2, max_length=1500)
    visual: str = Field(min_length=2, max_length=1500)
    tagline: str = Field(default="", max_length=180)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    local_timezone()
    init_database(DEFAULT_BRAND, timestamp())
    yield


app = FastAPI(
    title="Debelu Social Engine",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
class UploadLimitExceeded(Exception):
    pass


class UploadBodyLimitMiddleware:
    """Bound multipart bodies before Starlette spools them to temporary storage."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != "/api/assets" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    body = b'{"detail":"Multipart upload exceeds the server request limit."}'
                    await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
                    return
            except ValueError:
                body = b'{"detail":"Invalid Content-Length header."}'
                await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        consumed = 0
        response_started = False

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise UploadLimitExceeded
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        def exceeds_limit(exc: BaseException) -> bool:
            if isinstance(exc, UploadLimitExceeded):
                return True
            if isinstance(exc, BaseExceptionGroup):
                return any(exceeds_limit(child) for child in exc.exceptions)
            return False

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except BaseException as exc:
            if not exceeds_limit(exc):
                raise
            if not response_started:
                body = b'{"detail":"Multipart upload exceeds the server request limit."}'
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})


app.add_middleware(UploadBodyLimitMiddleware, max_bytes=max(settings.max_image_bytes, settings.max_video_bytes) + 2 * 1024 * 1024)
app.add_middleware(GZipMiddleware, minimum_size=900)


@app.middleware("http")
async def security_and_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        accepted_hosts = {request.headers.get("host", "").lower()}
        forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip().lower()
        if forwarded_host:
            accepted_hosts.add(forwarded_host)
        if origin:
            origin_host = urlsplit(origin).netloc.lower()
            if not origin_host or origin_host not in accepted_hosts:
                return JSONResponse({"detail": "Cross-origin requests are not accepted."}, status_code=403)

    public_paths = {"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}
    if path.startswith("/api/") and path not in public_paths:
        if not valid_session_cookie(request.cookies.get("debelu_session")):
            return JSONResponse({"detail": "Your session has expired. Sign in again."}, status_code=401)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if not settings.allow_preview_embed:
        response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    frame_policy = "'self' https://*.e2b.app https://arena.ai https://*.arena.ai" if settings.allow_preview_embed else "'none'"
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data:; connect-src 'self'; "
        f"frame-ancestors {frame_policy}; base-uri 'self'; form-action 'self'",
    )
    if settings.cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(PROJECT_ROOT / "index.html", media_type="text/html")


@app.get("/styles.css", include_in_schema=False)
def styles():
    return FileResponse(PROJECT_ROOT / "styles.css", media_type="text/css")


@app.get("/app.js", include_in_schema=False)
def javascript():
    return FileResponse(PROJECT_ROOT / "app.js", media_type="application/javascript")


@app.get("/api/health")
def health():
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        database_ok = True
    except Exception:
        logger.exception("Database health check failed")
        database_ok = False
    if not database_ok:
        raise HTTPException(status_code=503, detail="Database is unavailable.")
    return {"status": "ok", "database": "ok", "aiConfigured": bool(settings.ai_api_key)}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    client_key = request.client.host if request.client else "unknown"
    if login_is_limited(client_key):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Wait 15 minutes and try again.")
    if not hmac.compare_digest(payload.password, settings.app_password):
        note_login_failure(client_key)
        raise HTTPException(status_code=401, detail="Incorrect password.")
    clear_login_failures(client_key)
    response.set_cookie(
        key="debelu_session",
        value=make_session_cookie(),
        max_age=settings.session_hours * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )
    if settings.allow_preview_embed:
        # Partition the sandbox cookie to support the cross-site Arena preview iframe.
        response.headers["set-cookie"] = f"{response.headers['set-cookie']}; Partitioned"
    return {"authenticated": True, "aiConfigured": bool(settings.ai_api_key)}


@app.get("/api/auth/me")
def session_status(request: Request):
    return {"authenticated": valid_session_cookie(request.cookies.get("debelu_session"))}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("debelu_session", path="/", httponly=True, secure=settings.cookie_secure, samesite=settings.cookie_samesite)
    if settings.allow_preview_embed:
        response.headers["set-cookie"] = f"{response.headers['set-cookie']}; Partitioned"
    return {"authenticated": False}


@app.get("/api/workspace")
def get_workspace():
    with engine.connect() as connection:
        rows = connection.execute(
            select(content_ideas).order_by(content_ideas.c.created_at.desc())
        ).mappings().all()
        ideas = [serialize_idea(connection, row) for row in rows]
        profile = brand_profile(connection)
    return {
        "items": ideas,
        "brandProfile": profile,
        "socialAccounts": list_accounts(),
        "assets": list_assets(),
        "publishingJobs": list_publish_jobs(200),
        "publishingEnabled": settings.social_publishing_enabled,
        "publicMediaReady": bool(settings.public_base_url),
        "aiConfigured": bool(settings.ai_api_key),
        "aiModel": settings.ai_model if settings.ai_api_key else None,
        "timezone": settings.timezone,
    }


@app.get("/api/ideas")
def list_ideas(status: str | None = None, platform: str | None = None, search: str | None = None):
    if status and status not in {"IDEA", "READY_FOR_REVIEW", "APPROVED", "SCHEDULED", "PUBLISHED", "FAILED"}:
        raise HTTPException(status_code=422, detail="Unknown content status.")
    if platform and platform not in PLATFORMS:
        raise HTTPException(status_code=422, detail="Unknown platform.")
    with engine.connect() as connection:
        query = select(content_ideas)
        if status:
            query = query.where(content_ideas.c.status == status)
        if search:
            term = f"%{search[:100]}%"
            query = query.where(content_ideas.c.topic.ilike(term))
        rows = connection.execute(query.order_by(content_ideas.c.created_at.desc())).mappings().all()
        items = [serialize_idea(connection, row) for row in rows]
    if platform:
        items = [item for item in items if platform in item["variants"]]
    return {"items": items}


@app.post("/api/ideas", status_code=201)
def create_idea(payload: IdeaCreate):
    unsupported = set(payload.variants) - PLATFORMS
    if unsupported:
        raise HTTPException(status_code=422, detail=f"Unsupported platforms: {', '.join(sorted(unsupported))}.")
    now = timestamp()
    idea_id = str(uuid.uuid4())
    variants = payload.variants
    with engine.begin() as connection:
        connection.execute(insert(content_ideas).values(
            id=idea_id,
            topic=payload.topic,
            category=payload.category.upper(),
            audience=payload.audience,
            status="IDEA",
            created_at=now,
            updated_at=now,
        ))
        for platform in sorted(PLATFORMS):
            variant = variants.get(platform)
            connection.execute(insert(content_variants).values(
                idea_id=idea_id,
                platform=platform,
                format=variant.format if variant else "",
                body=variant.body if variant else "",
                caption=variant.caption if variant else "",
                updated_at=now,
            ))
        audit(connection, "idea_created", idea_id, {"category": payload.category.upper()})
        item = fetch_idea(connection, idea_id)
    return item


@app.post("/api/generation/week", status_code=201)
def generate_week():
    if not settings.ai_api_key:
        raise HTTPException(status_code=503, detail="AI generation is not configured. Add AI_API_KEY to the server .env file and restart the app.")
    local_now = datetime.now(local_timezone())
    plan_monday = local_now.date() - timedelta(days=local_now.weekday()) + timedelta(days=7)
    plan_dates = [(plan_monday + timedelta(days=offset)).isoformat() for offset in range(7)]
    plan_batch = plan_dates[0]
    with engine.connect() as connection:
        if connection.execute(select(content_ideas.c.id).where(content_ideas.c.plan_batch == plan_batch).limit(1)).first():
            raise HTTPException(status_code=409, detail="A plan already exists for that week. Review or remove it before generating another.")
        profile = brand_profile(connection)
    try:
        generated = generate_ai_week(profile, plan_batch, plan_dates)
        validated = []
        for idea in generated:
            if not isinstance(idea, dict):
                raise AIProviderError("The AI response contained an invalid idea. Try again.")
            topic = str(idea.get("topic", "")).strip()
            audience = str(idea.get("audience", "")).strip()
            category_raw = str(idea.get("category", "CYBERSECURITY")).strip().upper()
            category = category_raw if category_raw in CATEGORIES else "CYBERSECURITY"
            if not 8 <= len(topic) <= 120 or len(audience) > 160:
                raise AIProviderError("The AI response contained an invalid topic or audience. Try again.")
            variants_raw = idea.get("variants")
            if not isinstance(variants_raw, dict):
                raise AIProviderError("The AI response is missing platform versions. Try again.")
            variants = {platform: validate_variant_result(variants_raw.get(platform), platform) for platform in PLATFORMS}
            validated.append({"topic": topic, "audience": audience, "category": category, "variants": variants})
    except AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    now = timestamp()
    created_ids: list[str] = []
    try:
        with engine.begin() as connection:
            if connection.execute(select(content_ideas.c.id).where(content_ideas.c.plan_batch == plan_batch).limit(1)).first():
                raise HTTPException(status_code=409, detail="A plan for that week was created while generation was running.")
            for index, idea in enumerate(validated):
                idea_id = str(uuid.uuid4())
                created_ids.append(idea_id)
                connection.execute(insert(content_ideas).values(
                    id=idea_id,
                    topic=idea["topic"],
                    category=idea["category"],
                    audience=idea["audience"],
                    status="READY_FOR_REVIEW",
                    created_at=now,
                    updated_at=now,
                    suggested_for=plan_dates[index],
                    plan_batch=plan_batch,
                ))
                for platform, variant in idea["variants"].items():
                    connection.execute(insert(content_variants).values(
                        idea_id=idea_id,
                        platform=platform,
                        format=variant["format"],
                        body=variant["body"],
                        caption=variant["caption"],
                        updated_at=now,
                    ))
                audit(connection, "idea_generated", idea_id, {"model": settings.ai_model, "planBatch": plan_batch})
            items = [fetch_idea(connection, idea_id) for idea_id in created_ids]
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not save generated content")
        raise HTTPException(status_code=500, detail="The generated plan could not be saved. No content was approved or published.") from exc
    return {"items": items, "weekStart": plan_batch}


@app.get("/api/ideas/{idea_id}")
def get_idea(idea_id: str):
    with engine.connect() as connection:
        return fetch_idea(connection, idea_id)


@app.put("/api/ideas/{idea_id}")
def update_idea(idea_id: str, payload: IdeaUpdate):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        with engine.connect() as connection:
            return fetch_idea(connection, idea_id)
    variants_input = fields.pop("variants", None)
    if variants_input is not None:
        unsupported = set(variants_input) - PLATFORMS
        if unsupported:
            raise HTTPException(status_code=422, detail=f"Unsupported platforms: {', '.join(sorted(unsupported))}.")
    now = timestamp()
    with engine.begin() as connection:
        row = get_idea_row(connection, idea_id)
        if not row:
            raise HTTPException(status_code=404, detail="Content idea not found.")
        previous_status = row["status"]
        changes: dict[str, Any] = {}
        for field in ("topic", "category", "audience"):
            if field in fields:
                value = fields[field]
                if value is not None:
                    normalized = value.upper() if field == "category" else value
                    if normalized != row[field]:
                        changes[field] = normalized
        variant_changes: list[dict[str, str]] = []
        if variants_input is not None:
            for platform, variant in variants_input.items():
                existing = connection.execute(
                    select(content_variants).where(
                        content_variants.c.idea_id == idea_id,
                        content_variants.c.platform == platform,
                    )
                ).mappings().first()
                if not existing:
                    raise HTTPException(status_code=422, detail=f"Missing {platform} content version.")
                normalized = variant.model_dump() if isinstance(variant, VariantInput) else VariantInput.model_validate(variant).model_dump()
                if any(existing[key] != normalized[key] for key in ("format", "body", "caption")):
                    variant_changes.append({"platform": platform, **normalized})
        changed = bool(changes or variant_changes)
        if changed:
            if previous_status == "PUBLISHED" or connection.execute(select(publish_jobs.c.id).where(
                publish_jobs.c.idea_id == idea_id, publish_jobs.c.status == "PUBLISHED",
            ).limit(1)).first():
                raise HTTPException(status_code=409, detail="A platform has already published this content. Create a new idea for a follow-up post.")
            if connection.execute(select(publish_jobs.c.id).where(
                publish_jobs.c.idea_id == idea_id,
                publish_jobs.c.status.in_(["PROCESSING", "REMOTE_PROCESSING", "UNKNOWN"]),
            ).limit(1)).first():
                raise HTTPException(status_code=409, detail="A platform post is processing or has an unknown outcome. Verify its status before editing the content.")
            requires_reapproval = previous_status in {"APPROVED", "SCHEDULED", "FAILED"}
            values = {**changes, "updated_at": now}
            if requires_reapproval:
                values.update({"status": "READY_FOR_REVIEW", "scheduled_for": None, "approved_at": None, "approved_hash": None})
                connection.execute(update(publish_jobs).where(
                    publish_jobs.c.idea_id == idea_id,
                    publish_jobs.c.status.in_(["SCHEDULED", "RETRY"]),
                ).values(status="CANCELLED", next_attempt_at=None, last_error_code="content_changed", last_error="Content changed after approval.", updated_at=now))
            connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(**values))
            for variant in variant_changes:
                connection.execute(update(content_variants).where(
                    content_variants.c.idea_id == idea_id,
                    content_variants.c.platform == variant["platform"],
                ).values(format=variant["format"], body=variant["body"], caption=variant["caption"], updated_at=now))
            action = "content_edited_after_approval" if requires_reapproval else "content_updated"
            audit(connection, action, idea_id, {"fields": sorted(changes), "platforms": [v["platform"] for v in variant_changes]})
        item = fetch_idea(connection, idea_id)
    return item


@app.post("/api/ideas/{idea_id}/submit")
def submit_for_review(idea_id: str):
    now = timestamp()
    with engine.begin() as connection:
        row = get_idea_row(connection, idea_id)
        if not row:
            raise HTTPException(status_code=404, detail="Content idea not found.")
        if row["status"] == "READY_FOR_REVIEW":
            return fetch_idea(connection, idea_id)
        if row["status"] != "IDEA":
            raise HTTPException(status_code=409, detail="Only an idea can be sent to the review queue.")
        if not has_copy(connection, idea_id):
            raise HTTPException(status_code=422, detail="Add platform copy before sending this idea for review.")
        connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(
            status="READY_FOR_REVIEW", updated_at=now, approved_at=None, approved_hash=None,
        ))
        audit(connection, "submitted_for_review", idea_id)
        return fetch_idea(connection, idea_id)


@app.post("/api/ideas/{idea_id}/approve")
def approve_idea(idea_id: str):
    now = timestamp()
    with engine.begin() as connection:
        row = get_idea_row(connection, idea_id)
        if not row:
            raise HTTPException(status_code=404, detail="Content idea not found.")
        if row["status"] == "APPROVED":
            return fetch_idea(connection, idea_id)
        if row["status"] != "READY_FOR_REVIEW":
            raise HTTPException(status_code=409, detail="Send this idea to review before approving it.")
        if not has_copy(connection, idea_id):
            raise HTTPException(status_code=422, detail="Add copy to at least one platform before approval.")
        fingerprint = current_fingerprint(connection, row)
        connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(
            status="APPROVED", updated_at=now, approved_at=now, approved_hash=fingerprint,
        ))
        audit(connection, "approved", idea_id, {"fingerprint": fingerprint})
        return fetch_idea(connection, idea_id)


@app.post("/api/ideas/bulk-approve")
def bulk_approve():
    now = timestamp()
    approved_ids: list[str] = []
    skipped = 0
    with engine.begin() as connection:
        rows = connection.execute(select(content_ideas).where(content_ideas.c.status == "READY_FOR_REVIEW")).mappings().all()
        for row in rows:
            if not has_copy(connection, row["id"]):
                skipped += 1
                continue
            fingerprint = current_fingerprint(connection, row)
            connection.execute(update(content_ideas).where(content_ideas.c.id == row["id"]).values(
                status="APPROVED", updated_at=now, approved_at=now, approved_hash=fingerprint,
            ))
            audit(connection, "approved", row["id"], {"fingerprint": fingerprint, "bulk": True})
            approved_ids.append(row["id"])
        items = [fetch_idea(connection, idea_id) for idea_id in approved_ids]
    return {"items": items, "approvedCount": len(approved_ids), "skippedCount": skipped}


def validate_publish_targets(idea: dict[str, Any], targets: list[PublishTarget]) -> list[dict[str, Any]]:
    if not targets:
        return []  # Calendar-only planning remains available without connected accounts.
    if not settings.social_publishing_enabled:
        raise HTTPException(status_code=503, detail="Social publishing is disabled on this server.")
    platforms = [target.platform for target in targets]
    if any(platform not in PLATFORMS for platform in platforms):
        raise HTTPException(status_code=422, detail="Select only Instagram, Threads or TikTok.")
    if len(set(platforms)) != len(platforms):
        raise HTTPException(status_code=422, detail="Select no more than one connected account per platform for a single schedule.")
    normalized: list[dict[str, Any]] = []
    for target in targets:
        variant = idea["variants"].get(target.platform)
        if not variant:
            raise HTTPException(status_code=422, detail=f"The {target.platform.title()} draft is missing.")
        assets = variant.get("assets", [])
        if not isinstance(assets, list):
            assets = []
        with engine.connect() as connection:
            account = connection.execute(select(social_accounts).where(social_accounts.c.id == target.accountId)).mappings().first()
        if not account or account["platform"] != target.platform:
            raise HTTPException(status_code=422, detail=f"Choose a connected {target.platform.title()} account.")
        if account["status"] != "CONNECTED":
            raise HTTPException(status_code=409, detail=f"Reconnect {target.platform.title()} before scheduling.")
        if not settings.provider_configured(target.platform):
            raise HTTPException(status_code=503, detail=f"{target.platform.title()} app credentials are no longer configured.")
        if target.platform == "instagram":
            if not assets:
                raise HTTPException(status_code=422, detail="Instagram publishing needs a JPEG image, an MP4 Reel, or a JPEG carousel attached to the draft.")
            mime_types = [asset["mimeType"] for asset in assets]
            valid = (len(assets) == 1 and mime_types[0] in {"image/jpeg", "video/mp4"}) or (2 <= len(assets) <= 10 and all(mime == "image/jpeg" for mime in mime_types))
            if not valid:
                raise HTTPException(status_code=422, detail="Instagram accepts one JPEG, one MP4 Reel, or 2–10 JPEG carousel images in this version.")
            if any(asset["mimeType"] == "image/jpeg" and int(asset.get("sizeBytes") or 0) > 8 * 1024 * 1024 for asset in assets):
                raise HTTPException(status_code=422, detail="Instagram JPEG images must be no larger than 8 MiB.")
            if len(assets) == 1 and assets[0]["mimeType"] == "video/mp4":
                duration = assets[0].get("durationSeconds")
                if not duration or not 3 <= duration <= 15 * 60:
                    raise HTTPException(status_code=422, detail="Instagram Reels need a verified duration between 3 seconds and 15 minutes.")
                if int(assets[0].get("sizeBytes") or 0) > 300 * 1024 * 1024:
                    raise HTTPException(status_code=422, detail="Instagram Reels must be no larger than 300 MiB.")
            if not (variant.get("caption") or variant.get("body")):
                raise HTTPException(status_code=422, detail="Add caption copy to the Instagram draft before scheduling.")
        elif target.platform == "threads":
            text = "\n\n".join(part.strip() for part in (variant.get("body", ""), variant.get("caption", "")) if part and part.strip())
            if not text or len(text.encode("utf-8")) > 500:
                raise HTTPException(status_code=422, detail="Threads needs post text no longer than 500 UTF-8 bytes.")
            if len(assets) > 20 or any(asset["mimeType"] not in {"image/jpeg", "image/png", "video/mp4"} for asset in assets):
                raise HTTPException(status_code=422, detail="Threads supports up to 20 JPEG/PNG images or MP4 videos in this publishing flow.")
            if any(asset["mimeType"] == "image/jpeg" and int(asset.get("sizeBytes") or 0) > 8 * 1024 * 1024 for asset in assets):
                raise HTTPException(status_code=422, detail="Threads JPEG images must be no larger than 8 MiB.")
            for asset in assets:
                if asset["mimeType"] == "video/mp4":
                    duration = asset.get("durationSeconds")
                    if not duration or not 0 < duration <= 5 * 60:
                        raise HTTPException(status_code=422, detail="Threads videos need a verified duration of no more than 5 minutes.")
                    if int(asset.get("sizeBytes") or 0) > 1024 * 1024 * 1024:
                        raise HTTPException(status_code=422, detail="Threads MP4 videos must be no larger than 1 GiB.")
        else:
            if target.tiktok is None:
                raise HTTPException(status_code=422, detail="Choose TikTok privacy and interaction settings before scheduling.")
            if len(assets) != 1 or assets[0]["mimeType"] != "video/mp4":
                raise HTTPException(status_code=422, detail="TikTok Direct Post needs one attached MP4 video.")
            options = target.tiktok.model_dump()
            if not options["consentGiven"]:
                raise HTTPException(status_code=422, detail="Explicit TikTok posting consent is required before scheduling.")
            if not (variant.get("caption") or variant.get("body")) or len((variant.get("caption") or variant.get("body"))) > 2200:
                raise HTTPException(status_code=422, detail="TikTok needs caption text between 1 and 2,200 characters.")
            info = account_creator_info(target.accountId)
            if not info["canPost"]:
                raise HTTPException(status_code=409, detail="This TikTok account cannot publish right now. Try again later.")
            if options["privacyLevel"] not in info["privacyLevelOptions"]:
                raise HTTPException(status_code=422, detail="Select one of the privacy levels returned by TikTok for this account.")
            if info["unauditedClient"] and options["privacyLevel"] != "SELF_ONLY":
                raise HTTPException(status_code=422, detail="TikTok restricts unaudited developer apps to SELF_ONLY visibility.")
            if info["commentDisabled"] and options["allowComment"]:
                raise HTTPException(status_code=422, detail="Comments are disabled in this TikTok account's settings.")
            if info["duetDisabled"] and options["allowDuet"]:
                raise HTTPException(status_code=422, detail="Duet is disabled in this TikTok account's settings.")
            if info["stitchDisabled"] and options["allowStitch"]:
                raise HTTPException(status_code=422, detail="Stitch is disabled in this TikTok account's settings.")
            duration = assets[0].get("durationSeconds")
            if not duration:
                raise HTTPException(status_code=422, detail="The MP4 duration is not verified. Install ffprobe and re-upload the video.")
            if info["maxVideoPostDurationSec"] and duration > info["maxVideoPostDurationSec"]:
                raise HTTPException(status_code=422, detail="This video exceeds the current TikTok account duration limit.")
            options["consentAt"] = timestamp()
            options["consentTextVersion"] = "tiktok-content-sharing-2026-08"
            options["creatorUsername"] = info["creatorUsername"]
        if assets and not settings.public_base_url:
            raise HTTPException(status_code=503, detail="Set APP_PUBLIC_URL to a publicly reachable HTTPS origin before scheduling social media assets.")
        normalized.append({"platform": target.platform, "accountId": target.accountId,
                           "tiktok": options if target.platform == "tiktok" else {}})
    return normalized


@app.post("/api/ideas/{idea_id}/schedule")
def schedule_idea(idea_id: str, payload: ScheduleRequest):
    scheduled_for = parse_scheduled_time(payload.scheduledFor)
    now = timestamp()
    with engine.connect() as connection:
        preview_row = get_idea_row(connection, idea_id)
        if not preview_row:
            raise HTTPException(status_code=404, detail="Content idea not found.")
        if preview_row["status"] not in {"APPROVED", "SCHEDULED"}:
            raise HTTPException(status_code=409, detail="Approve the latest version before scheduling it.")
        idea_snapshot = serialize_idea(connection, preview_row)
    publish_targets = validate_publish_targets(idea_snapshot, payload.targets)
    approval_was_stale = False

    with engine.begin() as connection:
        row = connection.execute(select(content_ideas).where(content_ideas.c.id == idea_id).with_for_update()).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Content idea not found.")
        if row["status"] not in {"APPROVED", "SCHEDULED"}:
            raise HTTPException(status_code=409, detail="Approve the latest version before scheduling it.")
        current_hash = current_fingerprint(connection, row)
        if not row["approved_hash"] or not hmac.compare_digest(row["approved_hash"], current_hash):
            connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(
                status="READY_FOR_REVIEW", scheduled_for=None, approved_at=None, approved_hash=None, updated_at=now,
            ))
            audit(connection, "approval_invalidated", idea_id, {"reason": "content_fingerprint_mismatch"})
            approval_was_stale = True
            scheduled_jobs = []
            item = fetch_idea(connection, idea_id)
        else:
            existing_jobs = connection.execute(select(publish_jobs.c.status).where(publish_jobs.c.idea_id == idea_id)).scalars().all()
            if any(status in {"PROCESSING", "REMOTE_PROCESSING", "PUBLISHED", "UNKNOWN"} for status in existing_jobs):
                raise HTTPException(status_code=409, detail="A platform post is already processing, published or has an unknown outcome. Do not resubmit it automatically.")
            connection.execute(update(publish_jobs).where(
                publish_jobs.c.idea_id == idea_id, publish_jobs.c.status.in_(["SCHEDULED", "RETRY"]),
            ).values(status="CANCELLED", next_attempt_at=None, last_error_code="schedule_replaced", last_error="Replaced by a new schedule.", updated_at=now))
            connection.execute(update(content_ideas).where(content_ideas.c.id == idea_id).values(
                status="SCHEDULED", scheduled_for=scheduled_for, updated_at=now,
            ))
            scheduled_jobs = schedule_jobs(connection, idea_snapshot, current_hash, scheduled_for, publish_targets, now)
            audit(connection, "scheduled", idea_id, {"scheduledFor": scheduled_for, "platforms": [target["platform"] for target in publish_targets]})
            item = fetch_idea(connection, idea_id)
    if approval_was_stale:
        raise HTTPException(status_code=409, detail="Content changed after approval. Review and approve it again.")
    item["scheduledJobs"] = scheduled_jobs
    return item


@app.delete("/api/ideas/{idea_id}", status_code=204)
def delete_idea(idea_id: str):
    with engine.begin() as connection:
        row = get_idea_row(connection, idea_id)
        if not row:
            raise HTTPException(status_code=404, detail="Content idea not found.")
        if row["status"] != "IDEA":
            raise HTTPException(status_code=409, detail="Only unsent ideas can be deleted. Move reviewed content through the workflow instead.")
        audit(connection, "idea_deleted", idea_id, {"topic": row["topic"]})
        connection.execute(delete(content_ideas).where(content_ideas.c.id == idea_id))
    return Response(status_code=204)


@app.get("/api/ideas/{idea_id}/history")
def idea_history(idea_id: str):
    with engine.connect() as connection:
        if not get_idea_row(connection, idea_id):
            raise HTTPException(status_code=404, detail="Content idea not found.")
        rows = connection.execute(
            select(audit_events).where(audit_events.c.idea_id == idea_id).order_by(audit_events.c.created_at.desc())
        ).mappings().all()
    return {"events": [
        {"id": row["id"], "action": row["action"], "actor": row["actor"], "details": json.loads(row["details"]), "createdAt": row["created_at"]}
        for row in rows
    ]}


@app.put("/api/brand-profile")
def update_brand_profile(payload: BrandProfileInput):
    now = timestamp()
    values = payload.model_dump()
    with engine.begin() as connection:
        connection.execute(update(brand_profiles).where(brand_profiles.c.id == "default").values(**values, updated_at=now))
        audit(connection, "brand_profile_updated", details={"updatedAt": now})
        profile = brand_profile(connection)
    return profile


@app.post("/api/ideas/{idea_id}/publish", status_code=202)
def publish_idea(idea_id: str, payload: PublishNowRequest):
    # The worker owns all provider network calls. "Publish now" is queued two minutes ahead
    # so the user has a visible, cancellable outbox window and retries remain auditable.
    due_at = timestamp(utc_now() + timedelta(minutes=2))
    result = schedule_idea(idea_id, ScheduleRequest(scheduledFor=due_at, targets=payload.targets))
    result["publishQueued"] = True
    return result


from backend import social_api  # noqa: E402
app.include_router(social_api.router)
