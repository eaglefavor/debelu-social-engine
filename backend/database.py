from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine

from backend.config import PROJECT_ROOT, settings

metadata = MetaData()

content_ideas = Table(
    "content_ideas",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("topic", String(240), nullable=False),
    Column("category", String(80), nullable=False, default="CYBERSECURITY"),
    Column("audience", String(240), nullable=False, default=""),
    Column("status", String(24), nullable=False, default="IDEA"),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("suggested_for", String(10), nullable=True),
    Column("scheduled_for", String(40), nullable=True),
    Column("plan_batch", String(10), nullable=True),
    Column("approved_at", String(40), nullable=True),
    Column("approved_hash", String(64), nullable=True),
    Column("published_at", String(40), nullable=True),
    Column("published_url", String(1000), nullable=True),
    CheckConstraint(
        "status IN ('IDEA', 'READY_FOR_REVIEW', 'APPROVED', 'SCHEDULED', 'PUBLISHED', 'FAILED')",
        name="ck_content_ideas_status",
    ),
)
Index("ix_content_ideas_status", content_ideas.c.status)
Index("ix_content_ideas_scheduled_for", content_ideas.c.scheduled_for)
Index("ix_content_ideas_plan_batch", content_ideas.c.plan_batch)

content_variants = Table(
    "content_variants",
    metadata,
    Column("idea_id", String(36), ForeignKey("content_ideas.id", ondelete="CASCADE"), primary_key=True),
    Column("platform", String(24), primary_key=True),
    Column("format", String(100), nullable=False, default=""),
    Column("body", Text, nullable=False, default=""),
    Column("caption", Text, nullable=False, default=""),
    Column("updated_at", String(40), nullable=False),
    CheckConstraint("platform IN ('instagram', 'threads', 'tiktok')", name="ck_content_variants_platform"),
)

brand_profiles = Table(
    "brand_profiles",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("industry", Text, nullable=False),
    Column("voice", Text, nullable=False),
    Column("avoid", Text, nullable=False),
    Column("visual", Text, nullable=False),
    Column("tagline", String(180), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("idea_id", String(36), ForeignKey("content_ideas.id", ondelete="SET NULL"), nullable=True),
    Column("actor", String(80), nullable=False, default="owner"),
    Column("action", String(80), nullable=False),
    Column("details", Text, nullable=False, default="{}"),
    Column("created_at", String(40), nullable=False),
)
Index("ix_audit_events_idea_created", audit_events.c.idea_id, audit_events.c.created_at)

social_accounts = Table(
    "social_accounts",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("platform", String(24), nullable=False),
    Column("external_account_id", String(200), nullable=False),
    Column("username", String(200), nullable=False, default=""),
    Column("display_name", String(240), nullable=False, default=""),
    Column("avatar_url", String(2048), nullable=False, default=""),
    Column("scopes", Text, nullable=False, default=""),
    Column("access_token_enc", Text, nullable=False),
    Column("refresh_token_enc", Text, nullable=False, default=""),
    Column("access_expires_at", String(40), nullable=True),
    Column("refresh_expires_at", String(40), nullable=True),
    Column("status", String(24), nullable=False, default="CONNECTED"),
    Column("last_error", String(240), nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("platform", "external_account_id", name="uq_social_accounts_external"),
    CheckConstraint("platform IN ('instagram', 'threads', 'tiktok')", name="ck_social_accounts_platform"),
    CheckConstraint("status IN ('CONNECTED', 'REAUTH_REQUIRED', 'DISCONNECTED')", name="ck_social_accounts_status"),
)
Index("ix_social_accounts_platform_status", social_accounts.c.platform, social_accounts.c.status)

oauth_states = Table(
    "oauth_states",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("state_hash", String(64), nullable=False, unique=True),
    Column("platform", String(24), nullable=False),
    Column("session_hash", String(64), nullable=False),
    Column("code_verifier_enc", Text, nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("used_at", String(40), nullable=True),
    CheckConstraint("platform IN ('instagram', 'threads', 'tiktok')", name="ck_oauth_states_platform"),
)
Index("ix_oauth_states_expiry", oauth_states.c.expires_at)

content_assets = Table(
    "content_assets",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("original_name", String(240), nullable=False),
    Column("mime_type", String(80), nullable=False),
    Column("storage_key", String(80), nullable=False, unique=True),
    Column("size_bytes", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("width", Integer, nullable=True),
    Column("height", Integer, nullable=True),
    Column("duration_seconds", Float, nullable=True),
    Column("created_at", String(40), nullable=False),
    CheckConstraint("size_bytes > 0", name="ck_content_assets_size_positive"),
)
Index("ix_content_assets_hash", content_assets.c.sha256)

variant_assets = Table(
    "variant_assets",
    metadata,
    Column("idea_id", String(36), nullable=False),
    Column("platform", String(24), nullable=False),
    Column("asset_id", String(36), ForeignKey("content_assets.id", ondelete="RESTRICT"), nullable=False),
    Column("position", Integer, nullable=False, default=0),
    ForeignKeyConstraint(["idea_id", "platform"], ["content_variants.idea_id", "content_variants.platform"], ondelete="CASCADE"),
    UniqueConstraint("idea_id", "platform", "asset_id", name="uq_variant_assets_asset"),
    UniqueConstraint("idea_id", "platform", "position", name="uq_variant_assets_position"),
    CheckConstraint("platform IN ('instagram', 'threads', 'tiktok')", name="ck_variant_assets_platform"),
)

publish_jobs = Table(
    "publish_jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("idempotency_key", String(64), nullable=False, unique=True),
    Column("idea_id", String(36), ForeignKey("content_ideas.id", ondelete="RESTRICT"), nullable=False),
    Column("account_id", String(36), ForeignKey("social_accounts.id", ondelete="RESTRICT"), nullable=False),
    Column("platform", String(24), nullable=False),
    Column("scheduled_for", String(40), nullable=False),
    Column("status", String(32), nullable=False, default="SCHEDULED"),
    Column("content_hash", String(64), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("provider_post_id", String(240), nullable=False, default=""),
    Column("provider_url", String(2048), nullable=False, default=""),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("poll_count", Integer, nullable=False, default=0),
    Column("next_attempt_at", String(40), nullable=True),
    Column("remote_deadline_at", String(40), nullable=True),
    Column("lease_owner", String(80), nullable=False, default=""),
    Column("lease_until", String(40), nullable=True),
    Column("last_error_code", String(100), nullable=False, default=""),
    Column("last_error", String(500), nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("published_at", String(40), nullable=True),
    Column("next_metrics_at", String(40), nullable=True),
    Column("metrics_error", String(500), nullable=False, default=""),
    CheckConstraint("platform IN ('instagram', 'threads', 'tiktok')", name="ck_publish_jobs_platform"),
    CheckConstraint(
        "status IN ('SCHEDULED', 'PROCESSING', 'REMOTE_PROCESSING', 'RETRY', 'PUBLISHED', 'FAILED', 'UNKNOWN', 'NEEDS_ATTENTION', 'CANCELLED')",
        name="ck_publish_jobs_status",
    ),
    CheckConstraint("attempt_count >= 0 AND poll_count >= 0", name="ck_publish_jobs_counts_nonnegative"),
)
Index("ix_publish_jobs_due", publish_jobs.c.status, publish_jobs.c.scheduled_for, publish_jobs.c.next_attempt_at)
Index("ix_publish_jobs_idea", publish_jobs.c.idea_id, publish_jobs.c.created_at)
Index("ix_publish_jobs_account", publish_jobs.c.account_id, publish_jobs.c.status)

analytics_snapshots = Table(
    "analytics_snapshots",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("observation_key", String(64), nullable=False, unique=True),
    Column("publish_job_id", String(36), ForeignKey("publish_jobs.id", ondelete="CASCADE"), nullable=False),
    Column("account_id", String(36), ForeignKey("social_accounts.id", ondelete="RESTRICT"), nullable=False),
    Column("platform", String(24), nullable=False),
    Column("metric", String(100), nullable=False),
    Column("value", Float, nullable=False),
    Column("period", String(40), nullable=False, default="lifetime"),
    Column("period_end", String(40), nullable=False),
    Column("collected_at", String(40), nullable=False),
    CheckConstraint("platform IN ('instagram', 'threads', 'tiktok')", name="ck_analytics_snapshots_platform"),
    CheckConstraint("value >= 0", name="ck_analytics_snapshots_value_nonnegative"),
)
Index("ix_analytics_snapshots_platform_metric", analytics_snapshots.c.platform, analytics_snapshots.c.metric, analytics_snapshots.c.period_end)

worker_heartbeats = Table(
    "worker_heartbeats",
    metadata,
    Column("component", String(64), primary_key=True),
    Column("worker_id", String(80), nullable=False),
    Column("status", String(24), nullable=False),
    Column("last_seen_at", String(40), nullable=False),
    Column("details", String(240), nullable=False, default=""),
)


def _sqlite_connection_config(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _make_engine() -> Engine:
    url = settings.database_url
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 5}
        if url.startswith("sqlite:///"):
            raw_path = url[len("sqlite:///"):]
            if raw_path and raw_path != ":memory:" and not raw_path.startswith("/"):
                (PROJECT_ROOT / Path(raw_path).parent).mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        event.listen(engine, "connect", _sqlite_connection_config)
        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


engine = _make_engine()


def init_database(default_brand: dict[str, str], now: str) -> None:
    # create_all is additive and preserves the phase-1 schema. Production upgrades are also
    # versioned through Alembic before the service starts; it is not a replacement for migrations.
    metadata.create_all(engine)
    with engine.begin() as connection:
        existing = connection.execute(
            select(brand_profiles.c.id).where(brand_profiles.c.id == "default")
        ).first()
        if not existing:
            connection.execute(brand_profiles.insert().values(id="default", updated_at=now, **default_brand))
