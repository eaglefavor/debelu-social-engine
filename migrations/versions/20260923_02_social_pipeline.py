"""Add encrypted social accounts, media assets, publishing outbox and metrics.

Revision ID: 20260923_02
Revises: 20260923_01
"""
from __future__ import annotations

from alembic import op

from backend.database import (
    analytics_snapshots,
    content_assets,
    metadata,
    oauth_states,
    publish_jobs,
    social_accounts,
    variant_assets,
    worker_heartbeats,
)

revision = "20260923_02"
down_revision = "20260923_01"
branch_labels = None
depends_on = None

TABLES = [social_accounts, oauth_states, content_assets, variant_assets, publish_jobs, analytics_snapshots, worker_heartbeats]


def upgrade() -> None:
    metadata.create_all(op.get_bind(), tables=TABLES, checkfirst=True)


def downgrade() -> None:
    # This removes the new social pipeline tables and is destructive to account/job history.
    for table in reversed(TABLES):
        table.drop(op.get_bind(), checkfirst=True)
