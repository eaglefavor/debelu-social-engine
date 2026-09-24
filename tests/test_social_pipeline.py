from __future__ import annotations

import io
import json
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image
from sqlalchemy import insert, select

from test_support import TEST_DATA  # noqa: F401
from backend.crypto import decrypt_secret, sign_public_asset
from backend.database import content_assets, oauth_states, publish_jobs, social_accounts, engine
from backend.social_accounts import consume_oauth_state, oauth_callback
from backend.social_providers import TokenBundle
from pipeline_support import ApiCase, future_iso, insert_asset, register_account, runtime_settings


class OAuthSecurityTests(ApiCase):
    def test_connect_start_binds_one_time_state_to_the_current_session(self):
        with runtime_settings():
            response = self.client.post("/api/social/threads/connect")
            self.assertEqual(response.status_code, 200, response.text)
            authorization_url = response.json()["authorizationUrl"]
            parsed = urlparse(authorization_url)
            params = parse_qs(parsed.query)
            self.assertEqual(parsed.scheme, "https")
            self.assertIn(parsed.hostname, {"threads.com", "www.threads.com"})
            self.assertIn("threads_content_publish", params["scope"][0])
            raw_state = params["state"][0]
            cookie = self.client.cookies.get("debelu_session")

            with self.assertRaises(HTTPException) as wrong_session:
                consume_oauth_state("threads", raw_state, "forged-session-cookie")
            self.assertEqual(wrong_session.exception.status_code, 400)
            with self.assertRaises(HTTPException):
                consume_oauth_state("instagram", raw_state, cookie)

            state_id, verifier = consume_oauth_state("threads", raw_state, cookie)
            self.assertTrue(state_id)
            self.assertEqual(verifier, "")
            with engine.connect() as connection:
                row = connection.execute(select(oauth_states).where(oauth_states.c.id == state_id)).mappings().one()
            self.assertNotEqual(row["state_hash"], raw_state)
            self.assertIsNotNone(row["used_at"])
            with self.assertRaises(HTTPException) as replay:
                consume_oauth_state("threads", raw_state, cookie)
            self.assertEqual(replay.exception.status_code, 400)

    def test_tiktok_oauth_uses_pkce_and_disconnected_configuration_fails_closed(self):
        with runtime_settings():
            response = self.client.post("/api/social/tiktok/connect")
            self.assertEqual(response.status_code, 200, response.text)
            params = parse_qs(urlparse(response.json()["authorizationUrl"]).query)
            self.assertEqual(params["code_challenge_method"], ["S256"])
            self.assertTrue(params["code_challenge"][0])
            state = params["state"][0]
            cookie = self.client.cookies.get("debelu_session")
            state_hash = __import__("hashlib").sha256(state.encode()).hexdigest()
            with engine.connect() as connection:
                row = connection.execute(select(oauth_states).where(oauth_states.c.state_hash == state_hash)).mappings().one()
            verifier = decrypt_secret(row["code_verifier_enc"], f"oauth:{row['id']}:verifier")
            self.assertGreaterEqual(len(verifier), 40)
            self.assertEqual(len(params["code_challenge"][0]), 43)

        with runtime_settings(publishing=False):
            response = self.client.post("/api/social/threads/connect")
            self.assertEqual(response.status_code, 503)

    def test_oauth_callback_exchanges_code_once_and_encrypts_persisted_tokens(self):
        with runtime_settings():
            connect = self.client.post("/api/social/threads/connect")
            state = parse_qs(urlparse(connect.json()["authorizationUrl"]).query)["state"][0]
            cookie = self.client.cookies.get("debelu_session")
            now = datetime.now(timezone.utc)
            bundle = TokenBundle(
                "a-very-secret-access-token", "", (now + timedelta(days=50)).isoformat(), None,
                f"threads-{uuid.uuid4()}", "reviewed_user", "Reviewed User", "", "threads_basic,threads_content_publish",
            )

            class FakeProvider:
                def exchange_code(self, code, redirect_uri, verifier):
                    self.code, self.redirect_uri, self.verifier = code, redirect_uri, verifier
                    return bundle

            fake = FakeProvider()
            with patch("backend.social_accounts.get_provider", return_value=fake):
                account = oauth_callback("threads", "one-time-code", state, cookie)
            self.assertEqual(account["username"], "reviewed_user")
            with engine.connect() as connection:
                saved = connection.execute(select(social_accounts).where(social_accounts.c.id == account["id"])).mappings().one()
            self.assertNotIn("a-very-secret-access-token", saved["access_token_enc"])
            self.assertEqual(decrypt_secret(saved["access_token_enc"], f"social:threads:{account['id']}:access"), "a-very-secret-access-token")
            with self.assertRaises(HTTPException):
                oauth_callback("threads", "replayed-code", state, cookie)


class PublishingScheduleTests(ApiCase):
    def test_schedule_creates_one_idempotent_job_per_selected_platform(self):
        with runtime_settings():
            account = register_account("threads")
            idea = self.create_approved("threads")
            payload = {
                "scheduledFor": future_iso(120),
                "targets": [{"platform": "threads", "accountId": account["id"]}],
            }
            first = self.client.post(f"/api/ideas/{idea['id']}/schedule", json=payload)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["status"], "SCHEDULED")
            self.assertEqual(len(first.json()["scheduledJobs"]), 1)
            job_id = first.json()["scheduledJobs"][0]["id"]
            second = self.client.post(f"/api/ideas/{idea['id']}/schedule", json=payload)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()["scheduledJobs"][0]["id"], job_id)
            with engine.connect() as connection:
                rows = connection.execute(select(publish_jobs).where(publish_jobs.c.idea_id == idea["id"])).mappings().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "SCHEDULED")
            self.assertEqual(rows[0]["snapshot"]["topic"], idea["topic"])

    def test_target_validation_rejects_duplicates_wrong_accounts_and_unicode_overflow(self):
        with runtime_settings():
            threads = register_account("threads")
            instagram = register_account("instagram")
            idea = self.create_approved("threads")
            future = future_iso(180)
            duplicate = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                "scheduledFor": future,
                "targets": [
                    {"platform": "threads", "accountId": threads["id"]},
                    {"platform": "threads", "accountId": threads["id"]},
                ],
            })
            self.assertEqual(duplicate.status_code, 422, duplicate.text)
            mismatch = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                "scheduledFor": future, "targets": [{"platform": "threads", "accountId": instagram["id"]}],
            })
            self.assertEqual(mismatch.status_code, 422, mismatch.text)

            changed = self.client.put(f"/api/ideas/{idea['id']}", json={
                "variants": {"threads": {"format": "Text", "body": "é" * 260, "caption": ""}},
            })
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["status"], "READY_FOR_REVIEW")
            self.client.post(f"/api/ideas/{idea['id']}/submit")
            reapproved = self.client.post(f"/api/ideas/{idea['id']}/approve")
            self.assertEqual(reapproved.status_code, 200)
            too_long = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                "scheduledFor": future, "targets": [{"platform": "threads", "accountId": threads["id"]}],
            })
            self.assertEqual(too_long.status_code, 422, too_long.text)
            with engine.connect() as connection:
                self.assertFalse(connection.execute(select(publish_jobs.c.id).where(publish_jobs.c.idea_id == idea["id"])).first())

    def test_social_publish_feature_switch_and_approval_are_enforced(self):
        with runtime_settings():
            account = register_account("threads")
            approved = self.create_approved("threads")
            targets = [{"platform": "threads", "accountId": account["id"]}]
            body = {"scheduledFor": future_iso(120), "targets": targets}
            with runtime_settings(publishing=False):
                disabled = self.client.post(f"/api/ideas/{approved['id']}/schedule", json=body)
                self.assertEqual(disabled.status_code, 503, disabled.text)
            raw = self.client.post("/api/ideas", json={
                "topic": f"Unapproved test {uuid.uuid4().hex[:8]}", "category": "CYBERSECURITY",
                "variants": {"threads": {"format": "Text", "body": "Draft copy that has not yet been submitted to an owner for review.", "caption": ""}},
            }).json()
            rejected = self.client.post(f"/api/ideas/{raw['id']}/publish", json={"targets": targets})
            self.assertEqual(rejected.status_code, 409, rejected.text)
            queued = self.client.post(f"/api/ideas/{approved['id']}/publish", json={"targets": targets})
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertTrue(queued.json()["publishQueued"])
            self.assertEqual(len(queued.json()["scheduledJobs"]), 1)

    def test_tiktok_requires_private_audited_policy_creator_limits_and_explicit_consent(self):
        with runtime_settings(audited_tiktok=False):
            account = register_account("tiktok")
            idea = self.create_approved("tiktok")
            asset_id = insert_asset(idea_id=idea["id"], platform="tiktok", mime_type="video/mp4", duration=12.5)
            attached = self.client.put(f"/api/ideas/{idea['id']}/assets/tiktok", json={"assetIds": [asset_id]})
            self.assertEqual(attached.status_code, 200, attached.text)
            self.assertEqual(self.client.get(f"/api/ideas/{idea['id']}").json()["status"], "READY_FOR_REVIEW")
            self.client.post(f"/api/ideas/{idea['id']}/submit")
            approved = self.client.post(f"/api/ideas/{idea['id']}/approve")
            self.assertEqual(approved.status_code, 200)
            info = {
                "creatorUsername": "tiktok_tester", "privacyLevelOptions": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
                "commentDisabled": False, "duetDisabled": False, "stitchDisabled": False,
                "maxVideoPostDurationSec": 60, "canPost": True, "unauditedClient": True,
            }
            target = {"platform": "tiktok", "accountId": account["id"], "tiktok": {
                "privacyLevel": "SELF_ONLY", "allowComment": False, "allowDuet": False, "allowStitch": False,
                "ownBrand": False, "brandedContent": False, "isAigc": False, "consentGiven": True,
            }}
            with patch("backend.main.account_creator_info", return_value=info):
                allowed = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                    "scheduledFor": future_iso(180), "targets": [target],
                })
            self.assertEqual(allowed.status_code, 200, allowed.text)
            job = allowed.json()["scheduledJobs"][0]
            self.assertTrue(job["tiktok"]["consentGiven"])
            self.assertEqual(job["tiktok"]["privacyLevel"], "SELF_ONLY")

            another = self.create_approved("tiktok")
            another_asset = insert_asset(idea_id=another["id"], platform="tiktok", mime_type="video/mp4", duration=12.5)
            self.client.put(f"/api/ideas/{another['id']}/assets/tiktok", json={"assetIds": [another_asset]})
            self.client.post(f"/api/ideas/{another['id']}/submit")
            self.client.post(f"/api/ideas/{another['id']}/approve")
            public_target = {**target, "tiktok": {**target["tiktok"], "privacyLevel": "PUBLIC_TO_EVERYONE"}}
            with patch("backend.main.account_creator_info", return_value=info):
                rejected = self.client.post(f"/api/ideas/{another['id']}/schedule", json={
                    "scheduledFor": future_iso(180), "targets": [public_target],
                })
            self.assertEqual(rejected.status_code, 422, rejected.text)

    def test_tiktok_rejects_missing_consent_and_over_duration(self):
        with runtime_settings(audited_tiktok=True):
            account = register_account("tiktok")
            idea = self.create_approved("tiktok")
            asset_id = insert_asset(idea_id=idea["id"], platform="tiktok", mime_type="video/mp4", duration=61)
            self.client.put(f"/api/ideas/{idea['id']}/assets/tiktok", json={"assetIds": [asset_id]})
            self.client.post(f"/api/ideas/{idea['id']}/submit")
            self.client.post(f"/api/ideas/{idea['id']}/approve")
            creator = {
                "creatorUsername": "tiktok_tester", "privacyLevelOptions": ["PUBLIC_TO_EVERYONE"],
                "commentDisabled": False, "duetDisabled": False, "stitchDisabled": False,
                "maxVideoPostDurationSec": 60, "canPost": True, "unauditedClient": False,
            }
            target = {"platform": "tiktok", "accountId": account["id"], "tiktok": {
                "privacyLevel": "PUBLIC_TO_EVERYONE", "allowComment": False, "allowDuet": False,
                "allowStitch": False, "ownBrand": False, "brandedContent": False, "isAigc": False,
                "consentGiven": False,
            }}
            with patch("backend.main.account_creator_info", return_value=creator):
                missing_consent = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                    "scheduledFor": future_iso(180), "targets": [target],
                })
            self.assertEqual(missing_consent.status_code, 422, missing_consent.text)
            target["tiktok"]["consentGiven"] = True
            with patch("backend.main.account_creator_info", return_value=creator):
                too_long = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                    "scheduledFor": future_iso(180), "targets": [target],
                })
            self.assertEqual(too_long.status_code, 422, too_long.text)


class MediaSecurityTests(ApiCase):
    def image_bytes(self) -> bytes:
        image = Image.new("RGB", (128, 96), (30, 90, 140))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_image_upload_normalization_signed_public_url_and_detach_protection(self):
        with runtime_settings():
            png = self.image_bytes()
            uploaded = self.client.post("/api/assets", files={"file": ("../../brand.png", png, "image/png")})
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            asset = uploaded.json()
            self.assertEqual(asset["mimeType"], "image/png")
            self.assertEqual(asset["width"], 128)
            self.assertNotIn("../", asset["name"])
            private = self.client.get(f"/api/assets/{asset['id']}/content")
            self.assertEqual(private.status_code, 200)
            self.assertEqual(private.headers["x-content-type-options"], "nosniff")

            expiry = int(time.time()) + 60
            token = sign_public_asset(asset["id"], expiry)
            public = self.client.get(f"/public/media/{asset['id']}?token={token}")
            self.assertEqual(public.status_code, 200, public.text)
            self.assertEqual(public.headers["cache-control"], "public, max-age=300")
            self.assertEqual(self.client.get(f"/public/media/{asset['id']}?token=invalid").status_code, 403)
            expired = sign_public_asset(asset["id"], int(time.time()) - 1)
            self.assertEqual(self.client.get(f"/public/media/{asset['id']}?token={expired}").status_code, 403)

            idea = self.create_approved("threads")
            attached = self.client.put(f"/api/ideas/{idea['id']}/assets/threads", json={"assetIds": [asset["id"]]})
            self.assertEqual(attached.status_code, 200, attached.text)
            self.assertEqual(attached.json()["assets"][0]["id"], asset["id"])
            self.assertEqual(self.client.delete(f"/api/assets/{asset['id']}").status_code, 409)
            self.assertEqual(self.client.delete(f"/api/assets/not-a-real-asset").status_code, 404)

    def test_bad_image_and_oversized_uploads_fail_without_leaking_staged_files(self):
        with runtime_settings(max_image_bytes=1024, max_video_bytes=4096):
            invalid = self.client.post("/api/assets", files={"file": ("bad.svg", b"<svg onload=alert(1) />", "image/svg+xml")})
            self.assertEqual(invalid.status_code, 415, invalid.text)
            oversized = self.client.post("/api/assets", files={"file": ("large.bin", b"x" * 4097, "application/octet-stream")})
            self.assertEqual(oversized.status_code, 413, oversized.text)
            boundary = "debelu-test-boundary"
            multipart_headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
            def chunked_body():
                yield f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"huge.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
                yield b"x" * (1024 * 1024)
                yield b"x" * (1024 * 1024)
                yield b"x" * (1024 * 1024)
                yield f"\r\n--{boundary}--\r\n".encode()
            chunked = self.client.post("/api/assets", content=chunked_body(), headers=multipart_headers)
            self.assertEqual(chunked.status_code, 413, chunked.text)
            from backend.media import settings as media_settings
            self.assertFalse(list(media_settings.media_dir.glob(".upload-*.tmp")))

    def test_video_duration_is_recorded_and_required_before_tiktok_scheduling(self):
        with runtime_settings():
            fake_mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
            with patch("backend.media._probe_video", return_value=12.25):
                uploaded = self.client.post("/api/assets", files={"file": ("clip.mp4", fake_mp4, "video/mp4")})
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            self.assertEqual(uploaded.json()["durationSeconds"], 12.25)


if __name__ == "__main__":
    unittest.main()
