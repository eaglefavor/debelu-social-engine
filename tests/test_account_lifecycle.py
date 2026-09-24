from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select, update

from test_support import TEST_DATA  # noqa: F401
from backend.crypto import decrypt_secret, encrypt_secret
from backend.database import content_ideas, publish_jobs, social_accounts, engine
from backend.social_accounts import get_access_token
from backend.social_providers import PlatformError, TokenBundle
from pipeline_support import ApiCase, future_iso, register_account, runtime_settings


class SocialAccountLifecycleTests(ApiCase):
    def test_token_encryption_uses_authenticated_context_and_rejects_tampering(self):
        with runtime_settings():
            encoded = encrypt_secret("never-log-this-social-token", "social:threads:account-1:access")
            self.assertNotIn("never-log-this-social-token", encoded)
            self.assertEqual(decrypt_secret(encoded, "social:threads:account-1:access"), "never-log-this-social-token")
            with self.assertRaises(ValueError):
                decrypt_secret(encoded, "social:instagram:account-1:access")
            pieces = encoded.split(".")
            # Change significant ciphertext bits; mutating only the final Base64 character can alter unused pad bits.
            tampered_data = ("A" if pieces[2][0] != "A" else "B") + pieces[2][1:]
            tampered = pieces[0] + "." + pieces[1] + "." + tampered_data
            with self.assertRaises(ValueError):
                decrypt_secret(tampered, "social:threads:account-1:access")

    def test_expiring_threads_token_refresh_updates_ciphertext_without_exposing_it(self):
        with runtime_settings():
            account = register_account("threads")
            with engine.begin() as connection:
                connection.execute(update(social_accounts).where(social_accounts.c.id == account["id"]).values(
                    access_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds"),
                ))
            now = datetime.now(timezone.utc)
            refreshed = TokenBundle(
                "rotated-access-token", "", (now + timedelta(days=50)).isoformat(timespec="seconds"), None,
                "", "", "", "", "",
            )
            provider = MagicMock()
            provider.refresh.return_value = refreshed
            with patch("backend.social_accounts.get_provider", return_value=provider):
                access = get_access_token(account["id"])
            self.assertEqual(access, "rotated-access-token")
            provider.refresh.assert_called_once()
            with engine.connect() as connection:
                stored = connection.execute(select(social_accounts).where(social_accounts.c.id == account["id"])).mappings().one()
            self.assertNotIn("rotated-access-token", stored["access_token_enc"])
            self.assertEqual(decrypt_secret(stored["access_token_enc"], f"social:threads:{account['id']}:access"), "rotated-access-token")
            self.assertEqual(stored["status"], "CONNECTED")

    def test_expired_account_requiring_reauthorization_is_marked_and_not_retried(self):
        with runtime_settings():
            account = register_account("threads")
            with engine.begin() as connection:
                connection.execute(update(social_accounts).where(social_accounts.c.id == account["id"]).values(
                    access_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds"),
                ))
            provider = MagicMock()
            provider.refresh.side_effect = PlatformError("threads", "invalid_grant", "Reconnect this account.", reauth_required=True)
            with patch("backend.social_accounts.get_provider", return_value=provider):
                with self.assertRaises(PlatformError) as error:
                    get_access_token(account["id"])
            self.assertTrue(error.exception.reauth_required)
            with engine.connect() as connection:
                stored = connection.execute(select(social_accounts).where(social_accounts.c.id == account["id"])).mappings().one()
            self.assertEqual(stored["status"], "REAUTH_REQUIRED")

    def test_disconnect_cancels_unsubmitted_jobs_rolls_calendar_back_and_revokes_after_local_disable(self):
        with runtime_settings():
            account = register_account("threads")
            idea = self.create_approved("threads")
            scheduled = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                "scheduledFor": future_iso(300),
                "targets": [{"platform": "threads", "accountId": account["id"]}],
            })
            self.assertEqual(scheduled.status_code, 200, scheduled.text)
            job_id = scheduled.json()["scheduledJobs"][0]["id"]
            provider = MagicMock()
            with patch("backend.social_accounts.get_provider", return_value=provider):
                disconnected = self.client.delete(f"/api/social/accounts/{account['id']}")
            self.assertEqual(disconnected.status_code, 200, disconnected.text)
            self.assertEqual(disconnected.json()["status"], "DISCONNECTED")
            with engine.connect() as connection:
                job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
                idea_row = connection.execute(select(content_ideas).where(content_ideas.c.id == idea["id"])).mappings().one()
                stored = connection.execute(select(social_accounts).where(social_accounts.c.id == account["id"])).mappings().one()
            self.assertEqual(job["status"], "CANCELLED")
            self.assertEqual(idea_row["status"], "APPROVED")
            self.assertEqual(stored["access_token_enc"], "")
            provider.revoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
