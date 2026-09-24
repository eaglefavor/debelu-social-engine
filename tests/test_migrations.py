from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, insert, inspect, select

from test_support import TEST_DATA  # noqa: F401
from backend.database import audit_events, brand_profiles, content_ideas, content_variants

ROOT = Path(__file__).resolve().parents[1]


def alembic_command() -> list[str]:
    """Resolve the Alembic CLI without assuming a repo-local virtualenv.

    Prefer the documented ``.venv`` install when present, then any ``alembic``
    on ``PATH``, and finally the interpreter running the tests.
    """
    for candidate in (ROOT / ".venv" / "bin" / "alembic", Path(sys.executable).with_name("alembic")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    on_path = shutil.which("alembic")
    if on_path:
        return [on_path]
    return [sys.executable, "-m", "alembic"]


def isolated_environment(database_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "APP_PASSWORD": "migration-test-password-change-me",
        "APP_SECRET": "migration-test-secret-is-distinct-and-32-bytes",
        "DATABASE_URL": database_url,
        "APP_PUBLIC_URL": "",
        "SOCIAL_TOKEN_ENCRYPTION_KEY": "",
        "SOCIAL_PUBLISHING_ENABLED": "false",
        "TIKTOK_DIRECT_POST_AUDITED": "false",
        "COOKIE_SECURE": "false",
        "ALLOW_PREVIEW_EMBED": "false",
    })
    for key in ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET", "THREADS_APP_ID", "THREADS_APP_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"):
        env[key] = ""
    return env


class MigrationTests(unittest.TestCase):
    def run_alembic(self, database_url: str, *arguments: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [*alembic_command(), *arguments], cwd=ROOT, env=isolated_environment(database_url),
            capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, f"Alembic failed:\n{result.stdout}\n{result.stderr}")
        return result

    def test_fresh_database_migrates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="debelu-migration-fresh-") as directory:
            database_url = f"sqlite:///{Path(directory) / 'fresh.db'}"
            self.run_alembic(database_url, "upgrade", "head")
            self.run_alembic(database_url, "upgrade", "head")
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                expected = {
                    "alembic_version", "content_ideas", "content_variants", "brand_profiles", "audit_events",
                    "social_accounts", "oauth_states", "content_assets", "variant_assets", "publish_jobs",
                    "analytics_snapshots", "worker_heartbeats",
                }
                self.assertTrue(expected.issubset(set(inspector.get_table_names())))
                job_columns = {column["name"] for column in inspector.get_columns("publish_jobs")}
                self.assertTrue({"next_metrics_at", "metrics_error", "lease_until", "snapshot"}.issubset(job_columns))
            finally:
                engine.dispose()

    def test_existing_phase_one_database_keeps_data_when_adopted(self):
        with tempfile.TemporaryDirectory(prefix="debelu-migration-phase1-") as directory:
            database_url = f"sqlite:///{Path(directory) / 'phase1.db'}"
            engine = create_engine(database_url)
            idea_id = str(uuid.uuid4())
            now = "2026-09-23T10:00:00+00:00"
            try:
                # Simulate the unversioned Phase 1 schema before social-pipeline tables existed.
                from backend.database import metadata
                metadata.create_all(engine, tables=[content_ideas, content_variants, brand_profiles, audit_events])
                with engine.begin() as connection:
                    connection.execute(insert(content_ideas).values(
                        id=idea_id, topic="Existing Phase 1 idea", category="CYBERSECURITY", audience="Teams",
                        status="IDEA", created_at=now, updated_at=now,
                    ))
                    connection.execute(insert(content_variants).values(
                        idea_id=idea_id, platform="threads", format="Text post", body="Preserve this draft.",
                        caption="", updated_at=now,
                    ))
            finally:
                engine.dispose()

            self.run_alembic(database_url, "upgrade", "head")
            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    idea = connection.execute(select(content_ideas).where(content_ideas.c.id == idea_id)).mappings().one()
                    variant = connection.execute(select(content_variants).where(content_variants.c.idea_id == idea_id)).mappings().one()
                self.assertEqual(idea["topic"], "Existing Phase 1 idea")
                self.assertEqual(variant["body"], "Preserve this draft.")
                self.assertIn("social_accounts", inspect(engine).get_table_names())
                self.assertIn("publish_jobs", inspect(engine).get_table_names())
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
