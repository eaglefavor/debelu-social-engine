from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.config import settings

PLATFORMS = {"instagram", "threads", "tiktok"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_after(seconds: int | float) -> str:
    return (utc_now() + timedelta(seconds=max(0, int(seconds)))).isoformat(timespec="seconds")


def parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    access_expires_at: str | None
    refresh_expires_at: str | None
    external_account_id: str
    username: str
    display_name: str
    avatar_url: str
    scopes: str


@dataclass(frozen=True)
class PublishReceipt:
    external_id: str
    permalink: str = ""
    complete: bool = True
    status_detail: str = ""


@dataclass(frozen=True)
class Metric:
    name: str
    value: float
    period: str = "lifetime"
    period_end: str = ""


class PlatformError(RuntimeError):
    """Provider failure with explicit retry/ambiguity semantics and no raw credential data."""

    def __init__(self, platform: str, code: str, message: str, *, retryable: bool = False,
                 ambiguous: bool = False, retry_after: int | None = None, reauth_required: bool = False):
        super().__init__(message)
        self.platform = platform
        self.code = code[:100]
        self.message = message[:500]
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.retry_after = retry_after
        self.reauth_required = reauth_required


class SocialProvider:
    platform = ""

    def __init__(self):
        self.timeout = httpx.Timeout(settings.social_http_timeout_seconds, connect=min(10, settings.social_http_timeout_seconds))

    def _request_json(self, method: str, url: str, *, params: dict | None = None, data: dict | None = None,
                      json_body: dict | None = None, headers: dict | None = None,
                      form: bool = False) -> dict[str, Any]:
        request_headers = {"Accept": "application/json", **(headers or {})}
        try:
            response = httpx.request(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                headers=request_headers,
                timeout=self.timeout,
                follow_redirects=False,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PlatformError(
                self.platform,
                type(exc).__name__,
                "The platform connection timed out or was interrupted. The result may be unknown; it will not be blindly resent.",
                retryable=False,
                ambiguous=method.upper() in {"POST", "PUT"},
            ) from exc
        except httpx.RequestError as exc:
            raise PlatformError(self.platform, type(exc).__name__, "The platform request could not be completed.", retryable=False) from exc
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        if response.status_code >= 400:
            status = response.status_code
            reauth = status == 401
            retryable = status == 429 or status in {408, 425, 500, 502, 503, 504}
            ambiguous = status >= 500 and method.upper() in {"POST", "PUT"}
            code = f"http_{status}"
            try:
                body = response.json()
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict):
                    code = str(err.get("code") or err.get("type") or code)[:100]
                elif isinstance(body, dict) and isinstance(body.get("error"), str):
                    code = str(body["error"])[:100]
            except (ValueError, json.JSONDecodeError):
                pass
            message = "The platform authorization expired. Reconnect this account." if reauth else f"The platform rejected the request (HTTP {status})."
            raise PlatformError(self.platform, code, message, retryable=retryable, ambiguous=ambiguous,
                                retry_after=retry_after, reauth_required=reauth)
        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise PlatformError(self.platform, "invalid_json", "The platform returned an invalid response.",
                                ambiguous=method.upper() in {"POST", "PUT"}) from exc
        if not isinstance(result, dict):
            raise PlatformError(self.platform, "invalid_response", "The platform returned an unexpected response.",
                                ambiguous=method.upper() in {"POST", "PUT"})
        return result

    def authorization_url(self, state: str, code_challenge: str = "") -> str:
        raise NotImplementedError

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str = "") -> TokenBundle:
        raise NotImplementedError

    def refresh(self, refresh_token: str, access_token: str = "") -> TokenBundle:
        raise NotImplementedError

    def publish(self, access_token: str, external_account_id: str, snapshot: dict[str, Any], assets: list[dict[str, Any]]) -> PublishReceipt:
        raise NotImplementedError

    def poll_publish(self, access_token: str, external_account_id: str, publish_id: str) -> PublishReceipt:
        return PublishReceipt(publish_id, complete=True)

    def creator_info(self, access_token: str) -> dict[str, Any]:
        raise PlatformError(self.platform, "unsupported", "This platform does not expose creator publishing settings.")

    def collect_metrics(self, access_token: str, external_account_id: str, external_post_id: str) -> list[Metric]:
        return []

    def revoke(self, access_token: str) -> None:
        """Optional provider-specific revocation; account deletion still removes local credentials."""

    def _bearer(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def _check_publishing_limit(self, endpoint: str, access_token: str) -> None:
        response = self._request_json(
            "GET", endpoint, params={"fields": "quota_usage,config"}, headers=self._bearer(access_token),
        )
        entry = _first_data(response)
        config = entry.get("config") if isinstance(entry, dict) else None
        try:
            usage = int(entry["quota_usage"])
            total = int(config["quota_total"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PlatformError(self.platform, "publishing_limit_unavailable",
                                "The platform publishing quota could not be verified; no post was sent.") from exc
        if usage < 0 or total <= 0:
            raise PlatformError(self.platform, "publishing_limit_unavailable",
                                "The platform returned invalid publishing quota data; no post was sent.")
        if usage >= total:
            raise PlatformError(self.platform, "publishing_limit_reached",
                                f"The {self.platform.title()} publishing quota is exhausted ({usage}/{total} in the rolling window). Wait for quota to reset before retrying.")


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(1, min(900, int(value)))
    except ValueError:
        return None


def _first_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", payload)
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _require_provider_success(platform: str, payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code", "provider_error"))
        if code.lower() not in {"ok", "success", "0"}:
            status = int(error.get("http_status", 400)) if str(error.get("http_status", "")).isdigit() else 400
            raise PlatformError(platform, code, "The platform did not accept the publishing request.",
                                retryable=status == 429 or status >= 500,
                                ambiguous=status >= 500)
    return payload


class InstagramProvider(SocialProvider):
    platform = "instagram"
    GRAPH_ROOT = "https://graph.instagram.com"
    OAUTH_ROOT = "https://www.instagram.com"
    TOKEN_ROOT = "https://api.instagram.com"

    def _graph(self, path: str) -> str:
        return f"{self.GRAPH_ROOT}/{settings.instagram_api_version}/{path.lstrip('/')}"

    def authorization_url(self, state: str, code_challenge: str = "") -> str:
        params = {
            "client_id": settings.instagram_app_id,
            "redirect_uri": settings.oauth_redirect_uri(self.platform),
            "response_type": "code",
            "scope": "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights",
            "state": state,
        }
        return f"{self.OAUTH_ROOT}/oauth/authorize?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str = "") -> TokenBundle:
        short = _first_data(self._request_json("POST", f"{self.TOKEN_ROOT}/oauth/access_token", data={
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        }, form=True))
        short_token = str(short.get("access_token", ""))
        if not short_token:
            raise PlatformError(self.platform, "token_missing", "Instagram did not return an access token.")
        long = self._request_json("GET", f"{self.GRAPH_ROOT}/{settings.instagram_api_version}/access_token", params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.instagram_app_secret,
            "access_token": short_token,
        })
        access = str(long.get("access_token", ""))
        expires = int(long.get("expires_in", 60 * 24 * 60 * 60))
        if not access:
            raise PlatformError(self.platform, "long_token_missing", "Instagram token exchange did not return a long-lived access token.")
        profile = _first_data(self._request_json("GET", self._graph("me"), params={
            "fields": "user_id,username,name,account_type,profile_picture_url",
            "access_token": access,
        }))
        external_id = str(profile.get("user_id") or profile.get("id") or short.get("user_id") or "")
        if not external_id:
            raise PlatformError(self.platform, "profile_missing", "Instagram did not return a professional account ID.")
        return TokenBundle(access, "", iso_after(expires), None, external_id,
                           str(profile.get("username", "")), str(profile.get("name", "")),
                           str(profile.get("profile_picture_url", "")), str(short.get("permissions", "")))

    def refresh(self, refresh_token: str, access_token: str = "") -> TokenBundle:
        result = self._request_json("GET", f"{self.GRAPH_ROOT}/{settings.instagram_api_version}/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": access_token,
        })
        new_token = str(result.get("access_token", ""))
        if not new_token:
            raise PlatformError(self.platform, "refresh_missing", "Instagram did not return a refreshed access token.")
        return TokenBundle(new_token, "", iso_after(result.get("expires_in", 60 * 24 * 60 * 60)), None, "", "", "", "", "")

    def publish(self, access_token: str, external_account_id: str, snapshot: dict[str, Any], assets: list[dict[str, Any]]) -> PublishReceipt:
        if not assets:
            raise PlatformError(self.platform, "media_required", "Instagram publishing requires at least one uploaded image or video.")
        if len(assets) > 10:
            raise PlatformError(self.platform, "too_many_assets", "Instagram carousel posts support at most 10 media items.")
        caption = str(snapshot["variant"].get("caption") or snapshot["variant"].get("body") or "").strip()
        if not caption:
            raise PlatformError(self.platform, "caption_required", "Instagram requires caption copy before publishing.")
        headers = self._bearer(access_token)
        item_containers: list[str] = []
        videos = [asset for asset in assets if asset["mime_type"].startswith("video/")]
        if videos and (len(videos) != 1 or len(assets) != 1):
            raise PlatformError(self.platform, "unsupported_media_mix", "Instagram video publishing currently accepts one Reel video per post.")
        if len(assets) > 1 and any(asset["mime_type"] != "image/jpeg" for asset in assets):
            raise PlatformError(self.platform, "carousel_format", "Instagram carousel assets must be JPEG images.")
        for asset in assets:
            size_bytes = int(asset.get("size_bytes") or 0)
            if asset["mime_type"] == "image/jpeg" and size_bytes > 8 * 1024 * 1024:
                raise PlatformError(self.platform, "image_too_large", "Instagram images must be no larger than 8 MiB.")
            if asset["mime_type"] == "video/mp4":
                duration = asset.get("duration_seconds")
                if not duration or not 3 <= float(duration) <= 15 * 60:
                    raise PlatformError(self.platform, "video_duration_invalid", "Instagram Reels require a verified duration from 3 seconds to 15 minutes.")
                if size_bytes > 300 * 1024 * 1024:
                    raise PlatformError(self.platform, "video_too_large", "Instagram Reels must be no larger than 300 MiB.")
        self._check_publishing_limit(self._graph(f"{external_account_id}/content_publishing_limit"), access_token)
        if len(assets) > 1:
            for asset in assets:
                child = self._request_json("POST", self._graph(f"{external_account_id}/media"), data={
                    "image_url": asset["public_url"], "is_carousel_item": "true",
                }, headers=headers)
                item_containers.append(_provider_id(self.platform, child, "id"))
            parent = self._request_json("POST", self._graph(f"{external_account_id}/media"), data={
                "media_type": "CAROUSEL", "children": ",".join(item_containers), "caption": caption,
            }, headers=headers)
        else:
            asset = assets[0]
            if asset["mime_type"] == "image/jpeg":
                params = {"image_url": asset["public_url"], "caption": caption}
            elif asset["mime_type"] == "video/mp4":
                params = {"media_type": "REELS", "video_url": asset["public_url"], "caption": caption, "share_to_feed": "true"}
            else:
                raise PlatformError(self.platform, "unsupported_media", "Instagram supports JPEG images or MP4 Reels in this publishing flow.")
            parent = self._request_json("POST", self._graph(f"{external_account_id}/media"), data=params, headers=headers)
        creation_id = _provider_id(self.platform, parent, "id")
        # Meta recommends one status check per minute for up to five minutes; the lease is 10 minutes by default.
        if videos:
            time.sleep(30)
            for attempt in range(5):
                status = self._request_json("GET", self._graph(creation_id), params={"fields": "status_code"}, headers=headers)
                code = str(status.get("status_code", "")).upper()
                if code == "FINISHED":
                    break
                if code in {"ERROR", "EXPIRED"}:
                    raise PlatformError(self.platform, f"container_{code.lower()}", "Instagram could not process the Reel media.")
                if code == "PUBLISHED":
                    raise PlatformError(self.platform, "container_already_published",
                                        "Instagram reports this container as already published; verify the account before retrying.", ambiguous=True)
                if code != "IN_PROGRESS":
                    raise PlatformError(self.platform, "container_status_unknown", "Instagram returned an unknown Reel container status; no publish call was made.")
                if attempt < 4:
                    time.sleep(60)
                else:
                    raise PlatformError(self.platform, "container_timeout", "Instagram was still processing the Reel after 5 minutes; no publish call was made.", retryable=True)
        published = self._request_json("POST", self._graph(f"{external_account_id}/media_publish"), data={"creation_id": creation_id}, headers=headers)
        post_id = _provider_id(self.platform, published, "id")
        permalink = ""
        try:
            permalink = str(self._request_json("GET", self._graph(post_id), params={"fields": "permalink"}, headers=headers).get("permalink", ""))
        except PlatformError:
            pass
        return PublishReceipt(post_id, permalink)

    def collect_metrics(self, access_token: str, external_account_id: str, external_post_id: str) -> list[Metric]:
        result = self._request_json("GET", self._graph(f"{external_post_id}/insights"), params={
            "metric": "reach,likes,comments,saved,shares,total_interactions",
        }, headers=self._bearer(access_token))
        return _parse_meta_metrics(result)


class ThreadsProvider(SocialProvider):
    platform = "threads"
    ROOT = "https://graph.threads.com/v1.0"

    def authorization_url(self, state: str, code_challenge: str = "") -> str:
        params = {
            "client_id": settings.threads_app_id,
            "redirect_uri": settings.oauth_redirect_uri(self.platform),
            "scope": "threads_basic,threads_content_publish,threads_manage_insights",
            "response_type": "code",
            "state": state,
        }
        return f"https://threads.com/oauth/authorize?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str = "") -> TokenBundle:
        short = self._request_json("POST", "https://graph.threads.com/oauth/access_token", data={
            "client_id": settings.threads_app_id,
            "client_secret": settings.threads_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        }, form=True)
        short_token = str(short.get("access_token", ""))
        if not short_token:
            raise PlatformError(self.platform, "token_missing", "Threads did not return an access token.")
        long = self._request_json("GET", "https://graph.threads.com/access_token", params={
            "grant_type": "th_exchange_token",
            "client_secret": settings.threads_app_secret,
            "access_token": short_token,
        })
        access = str(long.get("access_token", ""))
        if not access:
            raise PlatformError(self.platform, "long_token_missing", "Threads token exchange did not return a long-lived access token.")
        profile = self._request_json("GET", f"{self.ROOT}/me", params={
            "fields": "id,username,name,threads_profile_picture_url",
            "access_token": access,
        })
        external_id = str(profile.get("id") or long.get("user_id") or short.get("user_id") or "")
        if not external_id:
            raise PlatformError(self.platform, "profile_missing", "Threads did not return an account ID.")
        expires = int(long.get("expires_in", 60 * 24 * 60 * 60))
        return TokenBundle(access, "", iso_after(expires), None, external_id,
                           str(profile.get("username", "")), str(profile.get("name", "")),
                           str(profile.get("threads_profile_picture_url", "")),
                           "threads_basic,threads_content_publish,threads_manage_insights")

    def refresh(self, refresh_token: str, access_token: str = "") -> TokenBundle:
        result = self._request_json("GET", "https://graph.threads.com/refresh_access_token", params={
            "grant_type": "th_refresh_token", "access_token": access_token,
        })
        new_token = str(result.get("access_token", ""))
        if not new_token:
            raise PlatformError(self.platform, "refresh_missing", "Threads did not return a refreshed access token.")
        return TokenBundle(new_token, "", iso_after(result.get("expires_in", 60 * 24 * 60 * 60)), None, "", "", "", "", "")

    def _wait_for_container(self, access_token: str, container_id: str) -> None:
        # Meta recommends an average 30-second processing wait, then status checks at most once per minute for 5 minutes.
        time.sleep(30)
        for attempt in range(5):
            response = self._request_json("GET", f"{self.ROOT}/{container_id}",
                                          params={"fields": "status,error_message"},
                                          headers=self._bearer(access_token))
            status = str(response.get("status", "")).upper()
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                detail = str(response.get("error_message") or status)[:250]
                raise PlatformError(self.platform, f"container_{status.lower()}", f"Threads could not process this post: {detail}")
            if status == "PUBLISHED":
                raise PlatformError(self.platform, "container_already_published",
                                    "Threads reports this container as already published; verify the account before retrying.", ambiguous=True)
            if status != "IN_PROGRESS":
                raise PlatformError(self.platform, "container_status_unknown",
                                    "Threads returned an unknown container status; no publish call was made.")
            if attempt < 4:
                time.sleep(60)
        raise PlatformError(self.platform, "container_timeout",
                            "Threads was still processing the post after 5 minutes; no publish call was made.", retryable=True)

    def publish(self, access_token: str, external_account_id: str, snapshot: dict[str, Any], assets: list[dict[str, Any]]) -> PublishReceipt:
        variant = snapshot["variant"]
        text = "\n\n".join(part.strip() for part in (variant.get("body", ""), variant.get("caption", "")) if part and part.strip())
        if not text:
            raise PlatformError(self.platform, "text_required", "Threads requires post text.")
        if len(text.encode("utf-8")) > 500:
            raise PlatformError(self.platform, "text_too_long", "Threads posts are limited to 500 UTF-8 bytes.")
        if len(assets) > 20 or any(asset["mime_type"] not in {"image/jpeg", "image/png", "video/mp4"} for asset in assets):
            raise PlatformError(self.platform, "carousel_format", "Threads supports up to 20 JPEG/PNG images or MP4 videos.")
        for asset in assets:
            size_bytes = int(asset.get("size_bytes") or 0)
            if asset["mime_type"] in {"image/jpeg", "image/png"} and size_bytes > 8 * 1024 * 1024:
                raise PlatformError(self.platform, "image_too_large", "Threads images must be no larger than 8 MiB.")
            if asset["mime_type"] == "video/mp4":
                duration = asset.get("duration_seconds")
                if not duration or not 0 < float(duration) <= 5 * 60:
                    raise PlatformError(self.platform, "video_duration_invalid", "Threads videos require a verified duration of no more than 5 minutes.")
                if size_bytes > 1024 * 1024 * 1024:
                    raise PlatformError(self.platform, "video_too_large", "Threads videos must be no larger than 1 GiB.")
        self._check_publishing_limit(f"{self.ROOT}/{external_account_id}/threads_publishing_limit", access_token)
        headers = self._bearer(access_token)
        if not assets:
            container = self._request_json("POST", f"{self.ROOT}/{external_account_id}/threads", data={"media_type": "TEXT", "text": text}, headers=headers)
        elif len(assets) == 1:
            asset = assets[0]
            if asset["mime_type"] in {"image/jpeg", "image/png"}:
                content = {"media_type": "IMAGE", "image_url": asset["public_url"], "text": text}
            elif asset["mime_type"] == "video/mp4":
                content = {"media_type": "VIDEO", "video_url": asset["public_url"], "text": text}
            else:
                raise PlatformError(self.platform, "unsupported_media", "Threads supports JPEG/PNG images or MP4 videos in this publishing flow.")
            container = self._request_json("POST", f"{self.ROOT}/{external_account_id}/threads", data=content, headers=headers)
        else:
            if len(assets) > 20 or any(asset["mime_type"] not in {"image/jpeg", "video/mp4"} for asset in assets):
                raise PlatformError(self.platform, "carousel_format", "Threads carousels support 2–20 JPEG images or MP4 videos.")
            children: list[str] = []
            for asset in assets:
                media_type = "IMAGE" if asset["mime_type"].startswith("image/") else "VIDEO"
                params = {"media_type": media_type, "is_carousel_item": "true"}
                params["image_url" if media_type == "IMAGE" else "video_url"] = asset["public_url"]
                child = self._request_json("POST", f"{self.ROOT}/{external_account_id}/threads", data=params, headers=headers)
                children.append(_provider_id(self.platform, child, "id"))
            container = self._request_json("POST", f"{self.ROOT}/{external_account_id}/threads", data={
                "media_type": "CAROUSEL", "children": ",".join(children), "text": text,
            }, headers=headers)
        container_id = _provider_id(self.platform, container, "id")
        self._wait_for_container(access_token, container_id)
        published = self._request_json("POST", f"{self.ROOT}/{external_account_id}/threads_publish", data={"creation_id": container_id}, headers=headers)
        post_id = _provider_id(self.platform, published, "id")
        permalink = ""
        try:
            permalink = str(self._request_json("GET", f"{self.ROOT}/{post_id}", params={"fields": "permalink"}, headers=headers).get("permalink", ""))
        except PlatformError:
            pass
        return PublishReceipt(post_id, permalink)

    def collect_metrics(self, access_token: str, external_account_id: str, external_post_id: str) -> list[Metric]:
        result = self._request_json("GET", f"{self.ROOT}/{external_post_id}/insights", params={
            "metric": "views,likes,replies,reposts,quotes,shares",
            "access_token": access_token,
        })
        return _parse_meta_metrics(result)


class TikTokProvider(SocialProvider):
    platform = "tiktok"
    ROOT = "https://open.tiktokapis.com/v2"

    def authorization_url(self, state: str, code_challenge: str = "") -> str:
        params = {
            "client_key": settings.tiktok_client_key,
            "response_type": "code",
            "scope": "user.info.basic,video.publish,video.list",
            "redirect_uri": settings.oauth_redirect_uri(self.platform),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str = "") -> TokenBundle:
        result = self._request_json("POST", "https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }, form=True)
        data = _tiktok_data(self.platform, result)
        access = str(data.get("access_token", ""))
        refresh = str(data.get("refresh_token", ""))
        external_id = str(data.get("open_id", ""))
        if not access or not refresh or not external_id:
            raise PlatformError(self.platform, "token_missing", "TikTok did not return all required account credentials.")
        profile = self._request_json("GET", f"{self.ROOT}/user/info/", params={"fields": "open_id,display_name,avatar_url"}, headers=self._bearer(access))
        profile_data = _tiktok_data(self.platform, profile)
        return TokenBundle(access, refresh, iso_after(data.get("expires_in", 86400)),
                           iso_after(data.get("refresh_expires_in", 31536000)), external_id,
                           str(profile_data.get("display_name", "")), str(profile_data.get("display_name", "")),
                           str(profile_data.get("avatar_url", "")), str(data.get("scope", "")))

    def refresh(self, refresh_token: str, access_token: str = "") -> TokenBundle:
        result = self._request_json("POST", "https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, form=True)
        data = _tiktok_data(self.platform, result)
        access = str(data.get("access_token", ""))
        refresh = str(data.get("refresh_token", ""))
        if not access or not refresh:
            raise PlatformError(self.platform, "refresh_missing", "TikTok did not return refreshed account credentials.")
        return TokenBundle(access, refresh, iso_after(data.get("expires_in", 86400)),
                           iso_after(data.get("refresh_expires_in", 31536000)),
                           str(data.get("open_id", "")), "", "", "", str(data.get("scope", "")))

    def creator_info(self, access_token: str) -> dict[str, Any]:
        result = _tiktok_data(self.platform, self._request_json("POST", f"{self.ROOT}/post/publish/creator_info/query/",
                                                               json_body={}, headers=self._bearer(access_token)))
        return result

    def publish(self, access_token: str, external_account_id: str, snapshot: dict[str, Any], assets: list[dict[str, Any]]) -> PublishReceipt:
        if len(assets) != 1 or assets[0]["mime_type"] != "video/mp4":
            raise PlatformError(self.platform, "video_required", "TikTok Direct Post currently requires one MP4 video asset.")
        metadata = snapshot.get("tiktok") or {}
        if not metadata.get("consentAt") or metadata.get("privacyLevel") not in {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}:
            raise PlatformError(self.platform, "tiktok_consent_required", "TikTok requires explicit post consent and a user-selected privacy level.")
        if not settings.tiktok_direct_post_audited and metadata.get("privacyLevel") != "SELF_ONLY":
            raise PlatformError(self.platform, "unaudited_private_only", "This TikTok app has not passed audit; Direct Post is restricted to SELF_ONLY.")
        creator = self.creator_info(access_token)
        privacy_options = creator.get("privacy_level_options", [])
        if metadata["privacyLevel"] not in privacy_options:
            raise PlatformError(self.platform, "privacy_options_changed", "TikTok's current privacy options differ from the scheduled selection. Review and schedule again.")
        max_duration = creator.get("max_video_post_duration_sec")
        if max_duration and assets[0].get("duration_seconds") and assets[0]["duration_seconds"] > max_duration:
            raise PlatformError(self.platform, "video_too_long", "The video exceeds the maximum duration currently allowed for this TikTok account.")
        if max_duration and not assets[0].get("duration_seconds"):
            raise PlatformError(self.platform, "video_duration_unknown", "Video duration could not be verified; install ffprobe or upload a verified media file before posting.")
        variant = snapshot["variant"]
        title = str(variant.get("caption") or variant.get("body") or "").strip()
        if not title or len(title) > 2200:
            raise PlatformError(self.platform, "title_invalid", "TikTok needs caption text no longer than 2,200 characters.")
        # TikTok's consent flow is captured at scheduling; options have no implicit defaults.
        post_info = {
            "title": title,
            "privacy_level": metadata["privacyLevel"],
            "disable_comment": not bool(metadata.get("allowComment")),
            "disable_duet": not bool(metadata.get("allowDuet")),
            "disable_stitch": not bool(metadata.get("allowStitch")),
            "brand_content_toggle": bool(metadata.get("brandedContent")),
            "brand_organic_toggle": bool(metadata.get("ownBrand")),
            "is_aigc": bool(metadata.get("isAigc")),
        }
        result = _tiktok_data(self.platform, self._request_json("POST", f"{self.ROOT}/post/publish/video/init/",
            json_body={"post_info": post_info, "source_info": {"source": "PULL_FROM_URL", "video_url": assets[0]["public_url"]}},
            headers=self._bearer(access_token)))
        publish_id = str(result.get("publish_id", ""))
        if not publish_id:
            raise PlatformError(self.platform, "publish_id_missing", "TikTok did not return a publish status identifier.", ambiguous=True)
        return PublishReceipt(publish_id, complete=False, status_detail="Processing on TikTok")

    def poll_publish(self, access_token: str, external_account_id: str, publish_id: str) -> PublishReceipt:
        result = _tiktok_data(self.platform, self._request_json("POST", f"{self.ROOT}/post/publish/status/fetch/",
            json_body={"publish_id": publish_id}, headers=self._bearer(access_token)))
        status = str(result.get("status", "")).upper()
        if status == "PUBLISH_COMPLETE":
            post_ids = result.get("publicaly_available_post_id") or result.get("publicly_available_post_id") or []
            post_id = str(post_ids[0]) if isinstance(post_ids, list) and post_ids else publish_id
            return PublishReceipt(post_id, complete=True, status_detail=status)
        if status == "FAILED":
            error = result.get("fail_reason") or "TikTok processing failed."
            raise PlatformError(self.platform, "publish_failed", str(error)[:300])
        if status in {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "SEND_TO_USER_INBOX"}:
            return PublishReceipt(publish_id, complete=False, status_detail=status)
        raise PlatformError(self.platform, f"unknown_status_{status.lower()}", "TikTok returned an unknown publishing status.")

    def revoke(self, access_token: str) -> None:
        self._request_json("POST", "https://open.tiktokapis.com/v2/oauth/revoke/", data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "token": access_token,
        }, form=True)

    def collect_metrics(self, access_token: str, external_account_id: str, external_post_id: str) -> list[Metric]:
        result = _tiktok_data(self.platform, self._request_json("POST", f"{self.ROOT}/video/query/",
            params={"fields": "id,view_count,like_count,comment_count,share_count"},
            json_body={"filters": {"video_ids": [external_post_id]}}, headers=self._bearer(access_token)))
        videos = result.get("videos", [])
        if not videos:
            return []
        video = videos[0]
        return [Metric(name, float(video[name])) for name in ("view_count", "like_count", "comment_count", "share_count") if isinstance(video.get(name), (int, float))]


def _provider_id(platform: str, payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        data = _first_data(payload)
        value = data.get(key)
    if not value:
        raise PlatformError(platform, "provider_id_missing", "The platform did not return an ID required to continue publishing.", ambiguous=True)
    return str(value)


def _tiktok_data(platform: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("code", "ok")).lower() not in {"ok", "0", "success"}:
        code = str(error.get("code", "provider_error"))
        status = 429 if code == "rate_limit_exceeded" else 400
        raise PlatformError(platform, code, "TikTok rejected the request; check granted scopes, app review and media requirements.", retryable=status == 429)
    return data if isinstance(data, dict) else payload


def _parse_meta_metrics(payload: dict[str, Any]) -> list[Metric]:
    result: list[Metric] = []
    for row in payload.get("data", []) if isinstance(payload.get("data"), list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", ""))
        values = row.get("values")
        if isinstance(values, list) and values:
            # Keep the most recent measurement when a provider returns a time series.
            latest = values[-1]
            if isinstance(latest, dict) and isinstance(latest.get("value"), (int, float)):
                result.append(Metric(name, float(latest["value"]), str(row.get("period", "lifetime")), str(latest.get("end_time", ""))))
        elif isinstance(row.get("total_value"), dict) and isinstance(row["total_value"].get("value"), (int, float)):
            result.append(Metric(name, float(row["total_value"]["value"]), str(row.get("period", "lifetime"))))
    return result


def get_provider(platform: str) -> SocialProvider:
    if platform == "instagram":
        return InstagramProvider()
    if platform == "threads":
        return ThreadsProvider()
    if platform == "tiktok":
        return TikTokProvider()
    raise ValueError("Unsupported social platform.")
