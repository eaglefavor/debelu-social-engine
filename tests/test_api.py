from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from test_support import TEST_DATA  # noqa: F401
from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.security import make_session_cookie, valid_session_cookie  # noqa: E402


class SocialEngineAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.client.cookies.clear()

    def sign_in(self):
        response = self.client.post("/api/auth/login", json={"password": os.environ["APP_PASSWORD"]})
        self.assertEqual(response.status_code, 200, response.text)

    def create_idea(self, topic: str = "Practical secure defaults for product teams") -> dict:
        response = self.client.post("/api/ideas", json={
            "topic": topic,
            "category": "APPLICATION SECURITY",
            "audience": "Product teams",
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def add_copy(self, item: dict) -> dict:
        response = self.client.put(f"/api/ideas/{item['id']}", json={
            "variants": {
                "threads": {
                    "format": "Text post",
                    "body": "Secure defaults reduce avoidable risk by making the safer choice the easiest choice for a product team.",
                    "caption": "What default would you change first?",
                }
            }
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_signed_session_cookie_verification(self):
        for _ in range(40):
            token = make_session_cookie()
            self.assertTrue(valid_session_cookie(token))
            payload_part, signature_part = token.split(".", 1)
            changed_first = "A" if signature_part[0] != "A" else "B"
            self.assertFalse(valid_session_cookie(f"{payload_part}.{changed_first}{signature_part[1:]}"))

    def test_workspace_is_private_and_login_creates_session(self):
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)
        self.sign_in()
        self.assertEqual(self.client.get("/api/workspace").status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)

    def test_cross_origin_mutations_are_rejected(self):
        self.sign_in()
        response = self.client.post(
            "/api/ideas",
            headers={"Origin": "https://malicious.example"},
            json={"topic": "A cross-origin idea", "category": "CYBERSECURITY"},
        )
        self.assertEqual(response.status_code, 403)

    def test_review_schedule_and_edit_invalidates_approval(self):
        self.sign_in()
        item = self.add_copy(self.create_idea())
        submitted = self.client.post(f"/api/ideas/{item['id']}/submit")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["status"], "READY_FOR_REVIEW")

        approved = self.client.post(f"/api/ideas/{item['id']}/approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "APPROVED")
        self.assertTrue(approved.json()["approvedAt"])

        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        scheduled = self.client.post(f"/api/ideas/{item['id']}/schedule", json={"scheduledFor": future})
        self.assertEqual(scheduled.status_code, 200, scheduled.text)
        self.assertEqual(scheduled.json()["status"], "SCHEDULED")

        changed = self.client.put(f"/api/ideas/{item['id']}", json={"topic": "Secure defaults that teams can actually keep"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["status"], "READY_FOR_REVIEW")
        self.assertIsNone(changed.json()["scheduledFor"])
        history = self.client.get(f"/api/ideas/{item['id']}/history")
        self.assertEqual(history.status_code, 200)
        actions = [entry["action"] for entry in history.json()["events"]]
        self.assertIn("approved", actions)
        self.assertIn("scheduled", actions)
        self.assertIn("content_edited_after_approval", actions)

    def test_approval_requires_copy_and_schedule_requires_approval(self):
        self.sign_in()
        item = self.create_idea("How to make security review more useful")
        self.assertEqual(self.client.post(f"/api/ideas/{item['id']}/submit").status_code, 422)
        self.add_copy(item)
        self.client.post(f"/api/ideas/{item['id']}/submit")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        response = self.client.post(f"/api/ideas/{item['id']}/schedule", json={"scheduledFor": future})
        self.assertEqual(response.status_code, 409)

    def test_brand_profile_is_persisted_and_ai_is_explicitly_optional(self):
        self.sign_in()
        response = self.client.put("/api/brand-profile", json={
            "industry": "Cybersecurity and cloud",
            "voice": "Technical, warm and direct",
            "avoid": "Fearmongering and invented statistics",
            "visual": "Navy, electric blue and clean diagrams",
            "tagline": "SECURE · BUILD · SCALE",
        })
        self.assertEqual(response.status_code, 200, response.text)
        workspace = self.client.get("/api/workspace").json()
        self.assertEqual(workspace["brandProfile"]["voice"], "Technical, warm and direct")
        self.assertFalse(workspace["aiConfigured"])
        self.assertEqual(self.client.post("/api/generation/week").status_code, 503)


if __name__ == "__main__":
    unittest.main()
