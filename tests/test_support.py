"""Shared isolated environment for the test suite; never uses a user's configured database or keys."""

import atexit
import os
import tempfile
from pathlib import Path

TEST_DATA = tempfile.TemporaryDirectory(prefix="debelu-test-")
atexit.register(TEST_DATA.cleanup)
os.environ["APP_PASSWORD"] = "test-owner-password-change-this"
os.environ["APP_SECRET"] = "test-only-secret-should-be-at-least-32-bytes-long"
os.environ["DATABASE_URL"] = f"sqlite:///{Path(TEST_DATA.name) / 'test.db'}"
os.environ["AI_API_KEY"] = ""
os.environ.pop("OPENAI_API_KEY", None)
os.environ["COOKIE_SECURE"] = "false"
os.environ["COOKIE_SAMESITE"] = "lax"
os.environ["ALLOW_PREVIEW_EMBED"] = "false"
os.environ["APP_PUBLIC_URL"] = ""
os.environ["SOCIAL_TOKEN_ENCRYPTION_KEY"] = ""
os.environ["SOCIAL_PUBLISHING_ENABLED"] = "false"
os.environ["TIKTOK_DIRECT_POST_AUDITED"] = "false"
for name in ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET", "THREADS_APP_ID", "THREADS_APP_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"):
    os.environ[name] = ""
