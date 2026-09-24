from __future__ import annotations

import unittest
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_support import TEST_DATA
from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.config import settings as base_settings
from backend.database import content_assets, engine
from backend.main import app
from backend.social_accounts import persist_token_bundle
from backend.social_providers import TokenBundle


@contextmanager
def runtime_settings(*, publishing: bool = True, audited_tiktok: bool = False, **changes):
    values = {
        "public_base_url": "https://social.example.test",
        "token_encryption_key": "11" * 32,
        "social_publishing_enabled": publishing,
        "tiktok_direct_post_audited": audited_tiktok,
        "instagram_app_id": "instagram-test-app",
        "instagram_app_secret": "instagram-test-secret",
        "threads_app_id": "threads-test-app",
        "threads_app_secret": "threads-test-secret",
        "tiktok_client_key": "tiktok-test-key",
        "tiktok_client_secret": "tiktok-test-secret",
        "media_dir": Path(TEST_DATA.name) / "media",
    }
    values.update(changes)
    configured = replace(base_settings, **values)
    # The modules keep imported Settings references; patch every server-side consumer together.
    with ExitStack() as stack:
        for module in (
            "backend.config", "backend.crypto", "backend.social_accounts", "backend.social_providers",
            "backend.main", "backend.social_api", "backend.publisher", "backend.media", "backend.worker",
        ):
            stack.enter_context(patch(f"{module}.settings", configured))
        yield configured


def register_account(platform: str, *, external_id: str | None = None, username: str | None = None) -> dict:
    identity = external_id or f"test-{platform}-{uuid.uuid4()}"
    name = username or f"{platform}_tester"
    now = datetime.now(timezone.utc)
    bundle = TokenBundle(
        access_token=f"access-token-{identity}",
        refresh_token=f"refresh-token-{identity}",
        access_expires_at=(now + timedelta(days=50)).isoformat(timespec="seconds"),
        refresh_expires_at=(now + timedelta(days=200)).isoformat(timespec="seconds"),
        external_account_id=identity,
        username=name,
        display_name=f"Test {platform.title()} account",
        avatar_url="",
        scopes="basic,content_publish,manage_insights",
    )
    return persist_token_bundle(platform, bundle)


def insert_asset(*, idea_id: str | None = None, platform: str | None = None,
                 mime_type: str = "video/mp4", duration: float | None = 12.5,
                 asset_id: str | None = None, storage_key: str | None = None) -> str:
    asset_id = asset_id or str(uuid.uuid4())
    extension = ".mp4" if mime_type == "video/mp4" else ".jpg"
    key = storage_key or f"{asset_id.replace('-', '')}{extension}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with engine.begin() as connection:
        connection.execute(insert(content_assets).values(
            id=asset_id, original_name=f"fixture{extension}", mime_type=mime_type, storage_key=key,
            size_bytes=256, sha256="a" * 64, width=720 if mime_type.startswith("image/") else None,
            height=1280 if mime_type.startswith("image/") else None, duration_seconds=duration,
            created_at=now,
        ))
    return asset_id


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        super().tearDownClass()

    def setUp(self):
        self.client.cookies.clear()
        response = self.client.post("/api/auth/login", json={"password": "test-owner-password-change-this"})
        if response.status_code != 200:
            raise AssertionError(f"Could not sign into isolated test app: {response.status_code} {response.text}")

    def create_approved(self, platform: str = "threads", *, topic: str | None = None) -> dict:
        topic = topic or f"Verified {platform} test idea {uuid.uuid4().hex[:8]}"
        text = "A practical, human-reviewed draft with clear platform-specific wording and no unsupported claims."
        variants = {
            "instagram": {"format": "Image post", "body": "", "caption": ""},
            "threads": {"format": "Text post", "body": "", "caption": ""},
            "tiktok": {"format": "Short video", "body": "", "caption": ""},
        }
        variants[platform] = {"format": "Test format", "body": text, "caption": "A reviewable call to action."}
        created = self.client.post("/api/ideas", json={
            "topic": topic, "category": "CYBERSECURITY", "audience": "Product teams", "variants": variants,
        })
        if created.status_code != 201:
            raise AssertionError(f"Idea creation failed: {created.status_code} {created.text}")
        idea = created.json()
        submitted = self.client.post(f"/api/ideas/{idea['id']}/submit")
        if submitted.status_code != 200:
            raise AssertionError(f"Idea submission failed: {submitted.status_code} {submitted.text}")
        approved = self.client.post(f"/api/ideas/{idea['id']}/approve")
        if approved.status_code != 200:
            raise AssertionError(f"Idea approval failed: {approved.status_code} {approved.text}")
        return approved.json()


def future_iso(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
