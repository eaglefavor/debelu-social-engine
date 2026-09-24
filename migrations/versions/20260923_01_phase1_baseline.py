"""Adopt the phase-1 schema under Alembic without dropping existing tables.

Revision ID: 20260923_01
Revises:
"""
from __future__ import annotations

from alembic import op

from backend.database import audit_events, brand_profiles, content_ideas, content_variants, metadata

revision = "20260923_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This is intentionally idempotent so an existing phase-1 database can be adopted safely.
    metadata.create_all(op.get_bind(), tables=[content_ideas, content_variants, brand_profiles, audit_events], checkfirst=True)


def downgrade() -> None:
    # The original phase-1 schema predates migrations. Keep it intact on downgrade.
    pass
