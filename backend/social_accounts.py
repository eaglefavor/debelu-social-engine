from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, insert, select, update

from backend.config import settings
from backend.crypto import decrypt_secret, encrypt_secret, hash_oauth_state, session_fingerprint
from backend.database import audit_events, oauth_states, publish_jobs, social_accounts, engine
from backend.social_providers import PlatformError, TokenBundle, get_provider

OAUTH_STATE_LIFETIME_SECONDS = 600
TOKEN_REFRESH_SKEW_SECONDS = 15 * 60


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or now_utc()).isoformat(timespec="seconds")


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def is_platform_configured(platform: str) -> bool:
    return settings.provider_configured(platform)


def public_account(row) -> dict:
    return {
        "id": row["id"],
        "platform": row["platform"],
        "externalAccountId": row["external_account_id"],
        "username": row["username"],
        "displayName": row["display_name"],
        "avatarUrl": row["avatar_url"],
        "scopes": [scope.strip() for scope in (row["scopes"] or "").replace(" ", ",").split(",") if scope.strip()],
        "status": row["status"],
        "lastError": row["last_error"],
        "accessExpiresAt": row["access_expires_at"],
        "createdAt": row["created_at"],
    }


def list_accounts() -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(select(social_accounts).order_by(social_accounts.c.platform, social_accounts.c.created_at)).mappings().all()
    return [public_account(row) for row in rows]


def connect_start(platform: str, session_cookie: str | None) -> str:
    if platform not in {"instagram", "threads", "tiktok"}:
        raise HTTPException(status_code=404, detail="Unknown social platform.")
    if not is_platform_configured(platform):
        raise HTTPException(status_code=503, detail=f"{platform.title()} OAuth is not configured on the server.")
    if not settings.social_publishing_enabled:
        raise HTTPException(status_code=503, detail="Social connections are disabled. Set SOCIAL_PUBLISHING_ENABLED=true after configuring app review, HTTPS callbacks and encrypted token storage.")
    try:
        session_hash = session_fingerprint(session_cookie)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Sign in again before connecting a social account.") from exc
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48) if platform == "tiktok" else ""
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") if verifier else ""
    now = now_utc()
    state_id = str(uuid.uuid4())
    verifier_enc = encrypt_secret(verifier, f"oauth:{state_id}:verifier") if verifier else ""
    with engine.begin() as connection:
        connection.execute(delete(oauth_states).where(oauth_states.c.expires_at < stamp(now)))
        connection.execute(insert(oauth_states).values(
            id=state_id,
            state_hash=hash_oauth_state(state),
            platform=platform,
            session_hash=session_hash,
            code_verifier_enc=verifier_enc,
            created_at=stamp(now),
            expires_at=stamp(now + timedelta(seconds=OAUTH_STATE_LIFETIME_SECONDS)),
            used_at=None,
        ))
    return get_provider(platform).authorization_url(state, challenge)


def consume_oauth_state(platform: str, raw_state: str, session_cookie: str | None) -> tuple[str, str]:
    if not raw_state or len(raw_state) > 512:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state. Start the connection again.")
    try:
        expected_session_hash = session_fingerprint(session_cookie)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Sign in again to complete account authorization.") from exc
    now = stamp()
    with engine.begin() as connection:
        row = connection.execute(select(oauth_states).where(
            oauth_states.c.state_hash == hash_oauth_state(raw_state),
            oauth_states.c.platform == platform,
            oauth_states.c.session_hash == expected_session_hash,
            oauth_states.c.used_at.is_(None),
            oauth_states.c.expires_at > now,
        )).mappings().first()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid, expired, or already-used OAuth state. Start the connection again.")
        changed = connection.execute(update(oauth_states).where(
            oauth_states.c.id == row["id"], oauth_states.c.used_at.is_(None), oauth_states.c.expires_at > now,
        ).values(used_at=now)).rowcount
        if changed != 1:
            raise HTTPException(status_code=409, detail="This account authorization has already been processed.")
        verifier = decrypt_secret(row["code_verifier_enc"], f"oauth:{row['id']}:verifier") if row["code_verifier_enc"] else ""
    return row["id"], verifier


def persist_token_bundle(platform: str, bundle: TokenBundle) -> dict:
    if not bundle.external_account_id or not bundle.access_token:
        raise HTTPException(status_code=502, detail="The provider did not return usable account credentials.")
    now = stamp()
    with engine.begin() as connection:
        existing = connection.execute(select(social_accounts).where(
            social_accounts.c.platform == platform,
            social_accounts.c.external_account_id == bundle.external_account_id,
        )).mappings().first()
        account_id = existing["id"] if existing else str(uuid.uuid4())
        access_enc = encrypt_secret(bundle.access_token, f"social:{platform}:{account_id}:access")
        refresh_enc = encrypt_secret(bundle.refresh_token, f"social:{platform}:{account_id}:refresh") if bundle.refresh_token else ""
        values = {
            "platform": platform,
            "external_account_id": bundle.external_account_id,
            "username": bundle.username[:200],
            "display_name": bundle.display_name[:240],
            "avatar_url": bundle.avatar_url[:2048],
            "scopes": bundle.scopes[:1000],
            "access_token_enc": access_enc,
            "refresh_token_enc": refresh_enc,
            "access_expires_at": bundle.access_expires_at,
            "refresh_expires_at": bundle.refresh_expires_at,
            "status": "CONNECTED",
            "last_error": "",
            "updated_at": now,
        }
        if existing:
            connection.execute(update(social_accounts).where(social_accounts.c.id == account_id).values(**values))
            action = "social_account_reconnected"
        else:
            connection.execute(insert(social_accounts).values(id=account_id, created_at=now, **values))
            action = "social_account_connected"
        connection.execute(insert(audit_events).values(
            id=str(uuid.uuid4()), idea_id=None, actor="owner", action=action,
            details=f'{{"platform":"{platform}","accountId":"{account_id}"}}', created_at=now,
        ))
        result = connection.execute(select(social_accounts).where(social_accounts.c.id == account_id)).mappings().one()
    return public_account(result)


def get_account(account_id: str):
    with engine.connect() as connection:
        return connection.execute(select(social_accounts).where(social_accounts.c.id == account_id)).mappings().first()


def account_creator_info(account_id: str) -> dict:
    row = get_account(account_id)
    if not row:
        raise HTTPException(status_code=404, detail="Social account not found.")
    if row["status"] != "CONNECTED":
        raise HTTPException(status_code=409, detail="Reconnect this social account before publishing.")
    if row["platform"] != "tiktok":
        raise HTTPException(status_code=422, detail="Creator publishing controls are only available for TikTok.")
    token = get_access_token(account_id)
    try:
        info = get_provider("tiktok").creator_info(token)
    except PlatformError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    # Return only fields TikTok requires the user to review; never return a credential.
    return {
        "creatorUsername": str(info.get("creator_username", row["username"])),
        "creatorNickname": str(info.get("creator_nickname", row["display_name"])),
        "creatorAvatarUrl": str(info.get("creator_avatar_url", row["avatar_url"])),
        "privacyLevelOptions": [str(value) for value in info.get("privacy_level_options", []) if isinstance(value, str)],
        "commentDisabled": bool(info.get("comment_disabled", False)),
        "duetDisabled": bool(info.get("duet_disabled", False)),
        "stitchDisabled": bool(info.get("stitch_disabled", False)),
        "maxVideoPostDurationSec": int(info.get("max_video_post_duration_sec", 0) or 0),
        "canPost": bool(info.get("can_post_now", True)),
        "unauditedClient": not settings.tiktok_direct_post_audited,
    }


def get_access_token(account_id: str) -> str:
    with engine.connect() as connection:
        row = connection.execute(select(social_accounts).where(social_accounts.c.id == account_id)).mappings().first()
    if not row:
        raise PlatformError("unknown", "account_missing", "The connected social account no longer exists.")
    if row["status"] != "CONNECTED":
        raise PlatformError(row["platform"], "account_reauth_required", "Reconnect this social account before publishing.", reauth_required=True)
    platform = row["platform"]
    access = decrypt_secret(row["access_token_enc"], f"social:{platform}:{account_id}:access")
    refresh = decrypt_secret(row["refresh_token_enc"], f"social:{platform}:{account_id}:refresh") if row["refresh_token_enc"] else ""
    expiry = parse_stamp(row["access_expires_at"])
    if not expiry or expiry > now_utc() + timedelta(seconds=TOKEN_REFRESH_SKEW_SECONDS):
        return access
    if platform in {"instagram", "threads"}:
        refresh = access
    if not refresh:
        _mark_reauth(account_id, "Access token expired and no refresh token is available.")
        raise PlatformError(platform, "refresh_unavailable", "The social account token expired. Reconnect the account.", reauth_required=True)
    try:
        bundle = get_provider(platform).refresh(refresh, access)
    except PlatformError as exc:
        if exc.reauth_required:
            _mark_reauth(account_id, exc.message)
        raise
    now = stamp()
    with engine.begin() as connection:
        connection.execute(update(social_accounts).where(social_accounts.c.id == account_id).values(
            access_token_enc=encrypt_secret(bundle.access_token, f"social:{platform}:{account_id}:access"),
            refresh_token_enc=encrypt_secret(bundle.refresh_token, f"social:{platform}:{account_id}:refresh") if bundle.refresh_token else row["refresh_token_enc"],
            access_expires_at=bundle.access_expires_at,
            refresh_expires_at=bundle.refresh_expires_at or row["refresh_expires_at"],
            status="CONNECTED", last_error="", updated_at=now,
        ))
    return bundle.access_token


def _mark_reauth(account_id: str, message: str) -> None:
    with engine.begin() as connection:
        connection.execute(update(social_accounts).where(social_accounts.c.id == account_id).values(
            status="REAUTH_REQUIRED", last_error=message[:240], updated_at=stamp(),
        ))


def disconnect_account(account_id: str) -> dict:
    # Imported lazily to avoid a module cycle; the roll-up runs only after all queued jobs are cancelled.
    from backend.publisher import _update_idea_rollup

    now = stamp()
    with engine.begin() as connection:
        row = connection.execute(select(social_accounts).where(social_accounts.c.id == account_id).with_for_update()).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Social account not found.")
        active = connection.execute(select(publish_jobs.c.id).where(
            publish_jobs.c.account_id == account_id,
            (
                publish_jobs.c.status.in_(["PROCESSING", "REMOTE_PROCESSING", "UNKNOWN"])
                | ((publish_jobs.c.status == "RETRY") & (publish_jobs.c.provider_post_id != ""))
                | ((publish_jobs.c.status == "NEEDS_ATTENTION") & (publish_jobs.c.provider_post_id != ""))
            ),
        ).limit(1)).first()
        if active:
            raise HTTPException(status_code=409, detail="This account has a post in flight or an unresolved provider outcome. Resolve that job before disconnecting the account.")
        platform = row["platform"]
        access_ciphertext = row["access_token_enc"]
        affected_ideas = set(connection.execute(select(publish_jobs.c.idea_id).where(
            publish_jobs.c.account_id == account_id,
            publish_jobs.c.status.in_(["SCHEDULED", "RETRY"]),
        )).scalars().all())
        connection.execute(update(publish_jobs).where(
            publish_jobs.c.account_id == account_id,
            publish_jobs.c.status.in_(["SCHEDULED", "RETRY"]),
        ).values(status="CANCELLED", next_attempt_at=None, last_error_code="account_disconnected", last_error="Account disconnected before publishing.", updated_at=now))
        for idea_id in affected_ideas:
            _update_idea_rollup(connection, idea_id, now)
        connection.execute(update(social_accounts).where(social_accounts.c.id == account_id).values(
            status="DISCONNECTED", access_token_enc="", refresh_token_enc="", access_expires_at=None,
            refresh_expires_at=None, last_error="", updated_at=now,
        ))
        connection.execute(insert(audit_events).values(
            id=str(uuid.uuid4()), idea_id=None, actor="owner", action="social_account_disconnected",
            details=f'{{"platform":"{platform}","accountId":"{account_id}"}}', created_at=now,
        ))
        result = connection.execute(select(social_accounts).where(social_accounts.c.id == account_id)).mappings().one()
    # Revoke only after local scheduling is disabled, and never keep the DB transaction open during HTTP.
    try:
        access = decrypt_secret(access_ciphertext, f"social:{platform}:{account_id}:access") if access_ciphertext else ""
        if access:
            get_provider(platform).revoke(access)
    except (ValueError, PlatformError):
        # Local disconnection must succeed even if Meta/TikTok revocation is unavailable.
        pass
    return public_account(result)


def oauth_callback(platform: str, code: str, raw_state: str, session_cookie: str | None) -> dict:
    state_id, verifier = consume_oauth_state(platform, raw_state, session_cookie)
    if not code or len(code) > 4096:
        raise HTTPException(status_code=400, detail="The provider returned an invalid authorization code.")
    provider = get_provider(platform)
    try:
        bundle = provider.exchange_code(code, settings.oauth_redirect_uri(platform), verifier)
    except PlatformError as exc:
        raise HTTPException(status_code=502, detail=f"Could not complete {platform.title()} authorization: {exc.message}") from exc
    return persist_token_bundle(platform, bundle)


def cleanup_oauth_states(older_than: str | None = None) -> int:
    cutoff = older_than or stamp()
    with engine.begin() as connection:
        result = connection.execute(delete(oauth_states).where(oauth_states.c.expires_at < cutoff))
        return int(result.rowcount or 0)
