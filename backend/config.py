from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Load a small, dependency-free .env file without overriding process env."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_password: str
    app_secret: str
    database_url: str
    cookie_secure: bool
    cookie_samesite: str
    allow_preview_embed: bool
    session_hours: int
    timezone: str
    ai_api_key: str
    ai_base_url: str
    ai_model: str
    public_base_url: str
    token_encryption_key: str
    social_publishing_enabled: bool
    tiktok_direct_post_audited: bool
    media_dir: Path
    max_image_bytes: int
    max_video_bytes: int
    worker_poll_seconds: float
    worker_lease_seconds: int
    worker_max_attempts: int
    social_http_timeout_seconds: float
    instagram_app_id: str
    instagram_app_secret: str
    instagram_api_version: str
    threads_app_id: str
    threads_app_secret: str
    tiktok_client_key: str
    tiktok_client_secret: str
    project_root: Path = PROJECT_ROOT

    @classmethod
    def from_environment(cls) -> "Settings":
        password = os.environ.get("APP_PASSWORD", "")
        secret = os.environ.get("APP_SECRET", "")
        if len(password) < 12:
            raise RuntimeError("Set APP_PASSWORD in .env to a unique password of at least 12 characters.")
        if len(secret) < 32:
            raise RuntimeError("Set APP_SECRET in .env to a random secret of at least 32 characters.")
        if password.lower().startswith("replace-this") or secret.lower().startswith("replace-this"):
            raise RuntimeError("Replace the example APP_PASSWORD and APP_SECRET values before starting the app.")
        if password == secret:
            raise RuntimeError("APP_PASSWORD and APP_SECRET must be different values.")
        cookie_samesite = os.environ.get("COOKIE_SAMESITE", "lax").strip().lower()
        if cookie_samesite not in {"lax", "strict", "none"}:
            raise RuntimeError("COOKIE_SAMESITE must be lax, strict or none.")
        cookie_secure = env_bool("COOKIE_SECURE")
        if cookie_samesite == "none" and not cookie_secure:
            raise RuntimeError("COOKIE_SECURE=true is required when COOKIE_SAMESITE=none.")
        allow_preview_embed = env_bool("ALLOW_PREVIEW_EMBED")
        if allow_preview_embed and not cookie_secure:
            raise RuntimeError("COOKIE_SECURE=true is required when ALLOW_PREVIEW_EMBED=true.")

        try:
            session_hours = int(os.environ.get("SESSION_HOURS", "12"))
            max_image_bytes = int(os.environ.get("MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
            max_video_bytes = int(os.environ.get("MAX_VIDEO_BYTES", str(250 * 1024 * 1024)))
            worker_lease_seconds = int(os.environ.get("WORKER_LEASE_SECONDS", "600"))
            worker_max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "5"))
            worker_poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "5"))
            social_http_timeout_seconds = float(os.environ.get("SOCIAL_HTTP_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise RuntimeError("Session, upload, worker and HTTP timeout settings must be numeric.") from exc
        if not 1 <= session_hours <= 168:
            raise RuntimeError("SESSION_HOURS must be between 1 and 168.")
        if not 1024 <= max_image_bytes <= 100 * 1024 * 1024:
            raise RuntimeError("MAX_IMAGE_BYTES must be between 1 KiB and 100 MiB.")
        if not 1024 <= max_video_bytes <= 2 * 1024 * 1024 * 1024:
            raise RuntimeError("MAX_VIDEO_BYTES must be between 1 KiB and 2 GiB.")
        if not 30 <= worker_lease_seconds <= 3600 or not 1 <= worker_max_attempts <= 12:
            raise RuntimeError("Worker lease must be 30–3600 seconds and retry attempts 1–12.")
        if not 0.5 <= worker_poll_seconds <= 60 or not 1 <= social_http_timeout_seconds <= 180:
            raise RuntimeError("Worker polling or social HTTP timeout is outside safe bounds.")

        ai_base_url = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        parsed_ai_url = urlsplit(ai_base_url)
        if parsed_ai_url.scheme != "https" and parsed_ai_url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("AI_BASE_URL must use HTTPS (HTTP is allowed only for localhost development).")
        if not parsed_ai_url.hostname:
            raise RuntimeError("AI_BASE_URL must be an absolute URL with a host.")

        public_base_url = os.environ.get("APP_PUBLIC_URL", "").strip().rstrip("/")
        if public_base_url:
            parsed_public = urlsplit(public_base_url)
            if not parsed_public.hostname or parsed_public.username or parsed_public.password or parsed_public.query or parsed_public.fragment:
                raise RuntimeError("APP_PUBLIC_URL must be an absolute public origin without credentials, query or fragment.")
            if parsed_public.scheme != "https" and parsed_public.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise RuntimeError("APP_PUBLIC_URL must use HTTPS outside localhost.")
            if parsed_public.path not in {"", "/"}:
                raise RuntimeError("APP_PUBLIC_URL must be an origin only; do not include a path prefix.")

        encryption_key = os.environ.get("SOCIAL_TOKEN_ENCRYPTION_KEY", "").strip()
        if encryption_key:
            try:
                if len(bytes.fromhex(encryption_key)) != 32:
                    raise ValueError
            except ValueError as exc:
                raise RuntimeError("SOCIAL_TOKEN_ENCRYPTION_KEY must be exactly 64 hexadecimal characters (32 bytes).") from exc
        credential_pairs = (
            ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"),
            ("THREADS_APP_ID", "THREADS_APP_SECRET"),
            ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
        )
        for first, second in credential_pairs:
            if bool(os.environ.get(first, "").strip()) != bool(os.environ.get(second, "").strip()):
                raise RuntimeError(f"Configure both {first} and {second}, or leave both blank.")
        social_client_credentials = any(os.environ.get(key, "").strip() for pair in credential_pairs for key in pair)
        if social_client_credentials and (not public_base_url or not encryption_key):
            raise RuntimeError("Social OAuth requires APP_PUBLIC_URL and SOCIAL_TOKEN_ENCRYPTION_KEY; configure them before adding provider credentials.")

        return cls(
            app_password=password,
            app_secret=secret,
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./data/debelu.db"),
            cookie_secure=cookie_secure,
            cookie_samesite=cookie_samesite,
            allow_preview_embed=allow_preview_embed,
            session_hours=session_hours,
            timezone=os.environ.get("APP_TIMEZONE", "Africa/Lagos"),
            ai_api_key=os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY", ""),
            ai_base_url=ai_base_url,
            ai_model=os.environ.get("AI_MODEL", "gpt-4o-mini"),
            public_base_url=public_base_url,
            token_encryption_key=encryption_key,
            social_publishing_enabled=env_bool("SOCIAL_PUBLISHING_ENABLED"),
            tiktok_direct_post_audited=env_bool("TIKTOK_DIRECT_POST_AUDITED"),
            media_dir=Path(os.environ.get("MEDIA_DIR", str(PROJECT_ROOT / "data" / "media"))).expanduser().resolve(),
            max_image_bytes=max_image_bytes,
            max_video_bytes=max_video_bytes,
            worker_poll_seconds=worker_poll_seconds,
            worker_lease_seconds=worker_lease_seconds,
            worker_max_attempts=worker_max_attempts,
            social_http_timeout_seconds=social_http_timeout_seconds,
            instagram_app_id=os.environ.get("INSTAGRAM_APP_ID", "").strip(),
            instagram_app_secret=os.environ.get("INSTAGRAM_APP_SECRET", "").strip(),
            instagram_api_version=os.environ.get("INSTAGRAM_API_VERSION", "v26.0").strip(),
            threads_app_id=os.environ.get("THREADS_APP_ID", "").strip(),
            threads_app_secret=os.environ.get("THREADS_APP_SECRET", "").strip(),
            tiktok_client_key=os.environ.get("TIKTOK_CLIENT_KEY", "").strip(),
            tiktok_client_secret=os.environ.get("TIKTOK_CLIENT_SECRET", "").strip(),
        )

    def oauth_redirect_uri(self, platform: str) -> str:
        return f"{self.public_base_url}/api/social/{platform}/callback" if self.public_base_url else ""

    def provider_configured(self, platform: str) -> bool:
        if not self.public_base_url or not self.token_encryption_key:
            return False
        if platform == "instagram":
            return bool(self.instagram_app_id and self.instagram_app_secret)
        if platform == "threads":
            return bool(self.threads_app_id and self.threads_app_secret)
        if platform == "tiktok":
            return bool(self.tiktok_client_key and self.tiktok_client_secret)
        return False


settings = Settings.from_environment()
