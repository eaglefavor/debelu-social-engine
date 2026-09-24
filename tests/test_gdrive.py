"""Tests for the Drive client. Nothing here touches the network: every request
goes through an httpx MockTransport, and the service-account key is generated
in-process so no credential ever exists on disk.
"""

from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from test_support import TEST_DATA  # noqa: F401
from backend.gdrive import (
    DriveClient,
    DriveError,
    ServiceAccountToken,
    build_client,
    load_service_account,
)


def b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class DriveClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()
        cls.pem = cls.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("utf-8")
        cls.account = {
            "client_email": "debelu-bot@example.iam.gserviceaccount.com",
            "private_key": cls.pem,
            "token_uri": "https://oauth2.example.test/token",
        }

    def build(self, handler, scopes="https://www.googleapis.com/auth/drive"):
        transport = httpx.MockTransport(handler)
        token = ServiceAccountToken(self.account, scopes, transport=transport)
        return DriveClient(token, transport=transport)

    # -- credential handling ----------------------------------------------

    def test_service_account_loads_from_inline_json(self):
        parsed = load_service_account(json.dumps(self.account))
        self.assertEqual(parsed["client_email"], self.account["client_email"])

    def test_service_account_rejects_invalid_json_and_missing_fields(self):
        with self.assertRaises(DriveError):
            load_service_account("{not json")
        with self.assertRaises(DriveError):
            load_service_account(json.dumps({"client_email": "a@b.c"}))
        with self.assertRaises(DriveError):
            load_service_account("   ")

    def test_assertion_is_a_verifiable_rs256_jwt(self):
        token = ServiceAccountToken(self.account, "https://www.googleapis.com/auth/drive")
        header, claims, signature = token.assertion(now=1_700_000_000).split(".")
        # Raises InvalidSignature if the signing input or key is wrong.
        self.public_key.verify(
            b64url_decode(signature), f"{header}.{claims}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256(),
        )
        self.assertEqual(json.loads(b64url_decode(header)), {"alg": "RS256", "typ": "JWT"})
        payload = json.loads(b64url_decode(claims))
        self.assertEqual(payload["iss"], self.account["client_email"])
        self.assertEqual(payload["aud"], self.account["token_uri"])
        self.assertEqual(payload["exp"] - payload["iat"], 3600)
        self.assertEqual(payload["scope"], "https://www.googleapis.com/auth/drive")

    def test_multiple_scopes_are_space_joined(self):
        token = ServiceAccountToken(self.account, "https://a/one, https://b/two")
        _, claims, _ = token.assertion(now=1_700_000_000).split(".")
        self.assertEqual(json.loads(b64url_decode(claims))["scope"], "https://a/one https://b/two")

    def test_access_token_is_cached_across_requests(self):
        token_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                token_requests.append(request)
                return httpx.Response(200, json={"access_token": "cached-token", "expires_in": 3600})
            return httpx.Response(200, json={"files": []})

        client = self.build(handler)
        client.list_files("root")
        client.list_files("root")
        self.assertEqual(len(token_requests), 1, "the access token should be reused until it nears expiry")

    def test_token_exchange_failure_is_reported(self):
        token = ServiceAccountToken(self.account, "scope", transport=httpx.MockTransport(
            lambda request: httpx.Response(400, text="invalid_grant")))
        with self.assertRaises(DriveError) as caught:
            token.access_token()
        self.assertIn("token exchange failed", str(caught.exception))

    # -- folder handling ---------------------------------------------------

    def test_ensure_folder_creates_only_when_missing(self):
        created = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            if request.method == "GET":
                return httpx.Response(200, json={"files": [{"id": "existing-id", "name": "Backups"}]})
            created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "brand-new"})

        client = self.build(handler)
        self.assertEqual(client.ensure_folder("Backups", "root"), "existing-id")
        self.assertEqual(created, [], "an existing folder must not be recreated")

    def test_ensure_folder_creates_when_absent(self):
        created = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            if request.method == "GET":
                return httpx.Response(200, json={"files": []})
            created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "created-id"})

        client = self.build(handler)
        self.assertEqual(client.ensure_folder("database", "root"), "created-id")
        self.assertEqual(created[0]["mimeType"], "application/vnd.google-apps.folder")
        self.assertEqual(created[0]["parents"], ["root"])

    def test_folder_query_escapes_apostrophes(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            seen["q"] = request.url.params.get("q")
            return httpx.Response(200, json={"files": []})

        self.build(handler).find_folder("Esther's Backups")
        self.assertIn("Esther\\'s Backups", seen["q"])

    # -- transport behaviour ----------------------------------------------

    def test_rate_limit_is_retried_then_succeeds(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            attempts.append(request)
            if len(attempts) < 3:
                return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")
            return httpx.Response(200, json={"files": [{"id": "ok"}]})

        client = self.build(handler)
        with mock.patch("backend.gdrive.time.sleep"):
            self.assertEqual(len(client.list_files("root")), 1)
        self.assertEqual(len(attempts), 3)

    def test_client_error_is_raised_not_retried(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            attempts.append(request)
            return httpx.Response(404, text="notFound")

        with self.assertRaises(DriveError):
            self.build(handler).get_file("missing")
        self.assertEqual(len(attempts), 1, "a 404 is not transient and must not be retried")

    def test_network_failure_is_retried_then_raised(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            attempts.append(request)
            raise httpx.ConnectError("boom")

        with mock.patch("backend.gdrive.time.sleep"):
            with self.assertRaises(DriveError):
                self.build(handler).list_files("root")
        self.assertEqual(len(attempts), 4, "should exhaust its attempts before giving up")

    # -- uploads -----------------------------------------------------------

    def test_small_upload_uses_multipart(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            captured["url"] = str(request.url)
            captured["content_type"] = request.headers.get("content-type")
            return httpx.Response(200, json={"id": "uploaded-id"})

        file_id = self.build(handler).upload_bytes("backup.sqlite", b"small-bytes", "folder-1")
        self.assertEqual(file_id, "uploaded-id")
        self.assertIn("uploadType=multipart", captured["url"])
        self.assertTrue(captured["content_type"].startswith("multipart/related; boundary="))

    def test_large_upload_uses_a_resumable_session(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            seen.append(request)
            if request.method == "POST":
                return httpx.Response(200, headers={"Location": "https://upload.example.test/session"}, json={})
            return httpx.Response(200, json={"id": "resumable-id"})

        payload = b"x" * (9 * 1024 * 1024)
        file_id = self.build(handler).upload_bytes("big-video.mp4", payload, "folder-1", "video/mp4")
        self.assertEqual(file_id, "resumable-id")
        self.assertIn("uploadType=resumable", str(seen[0].url))
        self.assertEqual(str(seen[1].url), "https://upload.example.test/session")

    # -- downloads and exports --------------------------------------------

    def test_read_text_exports_native_sheets_as_csv(self):
        urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            urls.append(str(request.url))
            if request.url.path.endswith("/export"):
                return httpx.Response(200, content=b"topic\nRow one\n")
            return httpx.Response(200, json={"mimeType": "application/vnd.google-apps.spreadsheet"})

        content = self.build(handler).read_text("sheet-id")
        self.assertEqual(content, "topic\nRow one\n")
        self.assertTrue(any("/export" in url and "text%2Fcsv" in url for url in urls))

    def test_download_failure_names_the_file(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.example.test":
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            return httpx.Response(403, text="forbidden")

        with self.assertRaises(DriveError) as caught:
            self.build(handler).download_bytes("file-9")
        self.assertIn("file-9", str(caught.exception))

    def test_build_client_refuses_when_integration_is_disabled(self):
        with self.assertRaises(DriveError) as caught:
            build_client()
        self.assertIn("disabled", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
