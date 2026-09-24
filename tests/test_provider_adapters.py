from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from test_support import TEST_DATA  # noqa: F401
from backend.social_providers import (
    InstagramProvider, Metric, PlatformError, PublishReceipt, ThreadsProvider, TikTokProvider,
)
from pipeline_support import runtime_settings


class ProviderTransportTests(unittest.TestCase):
    def test_http_failures_classify_retry_ambiguity_and_reauthorization(self):
        provider = ThreadsProvider()
        response = MagicMock()
        response.status_code = 429
        response.headers = {"retry-after": "17"}
        response.json.return_value = {"error": {"code": "rate_limit"}}
        with patch("backend.social_providers.httpx.request", return_value=response) as request:
            with self.assertRaises(PlatformError) as limited:
                provider._request_json("POST", "https://graph.threads.com/v1.0/u/threads", data={"text": "x"})
        self.assertTrue(limited.exception.retryable)
        self.assertFalse(limited.exception.ambiguous)
        self.assertEqual(limited.exception.retry_after, 17)
        self.assertFalse(request.call_args.kwargs["follow_redirects"])

        response.status_code = 503
        response.headers = {}
        with patch("backend.social_providers.httpx.request", return_value=response):
            with self.assertRaises(PlatformError) as ambiguous:
                provider._request_json("POST", "https://graph.threads.com/v1.0/u/threads", data={"text": "x"})
        self.assertTrue(ambiguous.exception.retryable)
        self.assertTrue(ambiguous.exception.ambiguous)

        response.status_code = 401
        with patch("backend.social_providers.httpx.request", return_value=response):
            with self.assertRaises(PlatformError) as expired:
                provider._request_json("GET", "https://graph.threads.com/v1.0/me")
        self.assertTrue(expired.exception.reauth_required)
        self.assertFalse(expired.exception.ambiguous)

    def test_network_timeout_and_invalid_json_are_not_assumed_safe_to_resend(self):
        provider = InstagramProvider()
        with patch("backend.social_providers.httpx.request", side_effect=httpx.ReadTimeout("socket stalled")):
            with self.assertRaises(PlatformError) as timeout:
                provider._request_json("POST", "https://graph.instagram.com/v26.0/1/media")
        self.assertTrue(timeout.exception.ambiguous)
        self.assertFalse(timeout.exception.retryable)

        invalid = MagicMock()
        invalid.status_code = 200
        invalid.headers = {}
        invalid.json.side_effect = ValueError("bad JSON")
        with patch("backend.social_providers.httpx.request", return_value=invalid):
            with self.assertRaises(PlatformError) as response_error:
                provider._request_json("POST", "https://graph.instagram.com/v26.0/1/media")
        self.assertEqual(response_error.exception.code, "invalid_json")
        self.assertTrue(response_error.exception.ambiguous)

    def test_threads_container_flow_is_mocked_and_text_limit_counts_utf8_bytes(self):
        with runtime_settings():
            provider = ThreadsProvider()
            provider._request_json = MagicMock(side_effect=[
                {"data": [{"quota_usage": 42, "config": {"quota_total": 250, "quota_duration": 86400}}]},
                {"id": "container-1"}, {"status": "FINISHED", "id": "container-1"},
                {"id": "post-1"}, {"permalink": "https://www.threads.net/@owner/post/1"},
            ])
            with patch("backend.social_providers.time.sleep") as sleep:
                receipt = provider.publish("secret-token", "owner-1", {
                    "variant": {"body": "A reviewed Threads post", "caption": "A precise question?"},
                }, [])
            self.assertEqual(receipt, PublishReceipt("post-1", "https://www.threads.net/@owner/post/1"))
            self.assertEqual(provider._request_json.call_count, 5)
            sleep.assert_called_once_with(30)
            self.assertEqual(provider._request_json.call_args_list[0].args[0], "GET")
            self.assertIn("threads_publishing_limit", provider._request_json.call_args_list[0].args[1])
            self.assertEqual(provider._request_json.call_args_list[1].args[0], "POST")
            self.assertEqual(provider._request_json.call_args_list[2].args[0], "GET")
            self.assertEqual(provider._request_json.call_args_list[2].kwargs["params"]["fields"], "status,error_message")
            self.assertIn("threads_publish", provider._request_json.call_args_list[3].args[1])

            provider._request_json.side_effect = [
                {"data": [{"quota_usage": 1, "config": {"quota_total": 250, "quota_duration": 86400}}]},
                {"id": "container-2"}, {"status": "FINISHED", "id": "container-2"},
                {"id": "post-2"}, {"permalink": ""},
            ]
            at_limit = "é" * 250  # 500 UTF-8 bytes, matching Threads' character-counting rule.
            with patch("backend.social_providers.time.sleep"):
                accepted = provider.publish("token", "owner-1", {"variant": {"body": at_limit, "caption": ""}}, [])
            self.assertEqual(accepted.external_id, "post-2")

            too_long = "é" * 501
            with self.assertRaises(PlatformError) as error:
                provider.publish("token", "owner-1", {"variant": {"body": too_long, "caption": ""}}, [])
            self.assertEqual(error.exception.code, "text_too_long")

    def test_threads_accepts_official_png_image_posts(self):
        with runtime_settings():
            provider = ThreadsProvider()
            provider._request_json = MagicMock(side_effect=[
                {"data": [{"quota_usage": 5, "config": {"quota_total": 250, "quota_duration": 86400}}]},
                {"id": "png-container"}, {"status": "FINISHED"}, {"id": "png-post"}, {"permalink": ""},
            ])
            with patch("backend.social_providers.time.sleep"):
                receipt = provider.publish("token", "threads-user", {"variant": {"body": "PNG post"}}, [{
                    "mime_type": "image/png", "size_bytes": 1024,
                    "public_url": "https://social.example.test/image.png",
                }])
            self.assertEqual(receipt.external_id, "png-post")
            self.assertEqual(provider._request_json.call_args_list[1].kwargs["data"]["media_type"], "IMAGE")
            self.assertEqual(provider._request_json.call_args_list[1].kwargs["data"]["image_url"], "https://social.example.test/image.png")

    def test_threads_container_polling_is_bounded_and_never_publishes_unready_media(self):
        with runtime_settings():
            quota = {"data": [{"quota_usage": 0, "config": {"quota_total": 250, "quota_duration": 86400}}]}
            provider = ThreadsProvider()
            provider._request_json = MagicMock(side_effect=[
                quota, {"id": "slow-container"}, *([{"status": "IN_PROGRESS"}] * 5),
            ])
            with patch("backend.social_providers.time.sleep") as sleep:
                with self.assertRaises(PlatformError) as timeout:
                    provider.publish("token", "threads-user", {"variant": {"body": "Reviewed post"}}, [])
            self.assertEqual(timeout.exception.code, "container_timeout")
            self.assertTrue(timeout.exception.retryable)
            self.assertFalse(timeout.exception.ambiguous)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60, 60, 60, 60])
            self.assertEqual(provider._request_json.call_count, 7)
            self.assertFalse(any(call.args[1].endswith("/threads_publish") for call in provider._request_json.call_args_list))

            failed = ThreadsProvider()
            failed._request_json = MagicMock(side_effect=[
                quota, {"id": "bad-container"}, {"status": "ERROR", "error_message": "FAILED_PROCESSING_VIDEO"},
            ])
            with patch("backend.social_providers.time.sleep"):
                with self.assertRaises(PlatformError) as rejected:
                    failed.publish("token", "threads-user", {"variant": {"body": "Reviewed post"}}, [])
            self.assertEqual(rejected.exception.code, "container_error")
            self.assertIn("FAILED_PROCESSING_VIDEO", rejected.exception.message)
            self.assertEqual(failed._request_json.call_count, 3)

    def test_instagram_carousel_container_order_and_media_constraints(self):
        with runtime_settings():
            provider = InstagramProvider()
            provider._request_json = MagicMock(side_effect=[
                {"data": [{"quota_usage": 25, "config": {"quota_total": 100, "quota_duration": 86400}}]},
                {"id": "child-a"}, {"id": "child-b"}, {"id": "carousel-parent"},
                {"id": "published-1"}, {"permalink": "https://www.instagram.com/p/one/"},
            ])
            receipt = provider.publish("token", "ig-user", {
                "variant": {"body": "", "caption": "A reviewable carousel caption."},
            }, [
                {"mime_type": "image/jpeg", "public_url": "https://social.example.test/a.jpg"},
                {"mime_type": "image/jpeg", "public_url": "https://social.example.test/b.jpg"},
            ])
            self.assertEqual(receipt.external_id, "published-1")
            parent_call = provider._request_json.call_args_list[3]
            self.assertEqual(parent_call.kwargs["data"]["children"], "child-a,child-b")
            self.assertEqual(parent_call.kwargs["data"]["media_type"], "CAROUSEL")
            with self.assertRaises(PlatformError) as invalid:
                provider.publish("token", "ig-user", {"variant": {"caption": "caption"}}, [
                    {"mime_type": "video/mp4", "public_url": "https://social.example.test/video.mp4"},
                    {"mime_type": "image/jpeg", "public_url": "https://social.example.test/image.jpg"},
                ])
            self.assertEqual(invalid.exception.code, "unsupported_media_mix")

    def test_instagram_reel_polling_waits_for_finished_and_is_bounded(self):
        with runtime_settings():
            quota = {"data": [{"quota_usage": 2, "config": {"quota_total": 50, "quota_duration": 86400}}]}
            provider = InstagramProvider()
            provider._request_json = MagicMock(side_effect=[
                quota, {"id": "reel-container"}, {"status_code": "IN_PROGRESS"},
                {"status_code": "FINISHED"}, {"id": "published-reel"},
                {"permalink": "https://www.instagram.com/reel/reel-1/"},
            ])
            with patch("backend.social_providers.time.sleep") as sleep:
                receipt = provider.publish("token", "ig-user", {"variant": {"caption": "Reviewed Reel"}}, [{
                    "mime_type": "video/mp4", "size_bytes": 4096, "duration_seconds": 12.5,
                    "public_url": "https://social.example.test/reel.mp4",
                }])
            self.assertEqual(receipt.external_id, "published-reel")
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60])
            self.assertIn("media_publish", provider._request_json.call_args_list[4].args[1])

            stalled = InstagramProvider()
            stalled._request_json = MagicMock(side_effect=[quota, {"id": "slow-reel"}] + [{"status_code": "IN_PROGRESS"}] * 5)
            with patch("backend.social_providers.time.sleep") as sleep:
                with self.assertRaises(PlatformError) as timeout:
                    stalled.publish("token", "ig-user", {"variant": {"caption": "Reviewed Reel"}}, [{
                        "mime_type": "video/mp4", "size_bytes": 4096, "duration_seconds": 12.5,
                        "public_url": "https://social.example.test/reel.mp4",
                    }])
            self.assertEqual(timeout.exception.code, "container_timeout")
            self.assertTrue(timeout.exception.retryable)
            self.assertFalse(any(call.args[1].endswith("/media_publish") for call in stalled._request_json.call_args_list))
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 60, 60, 60, 60])

    def test_meta_quota_preflight_blocks_publish_at_limit_or_when_quota_is_unreadable(self):
        with runtime_settings():
            instagram = InstagramProvider()
            instagram._request_json = MagicMock(return_value={"data": [{
                "quota_usage": 100, "config": {"quota_total": 100, "quota_duration": 86400},
            }]})
            with self.assertRaises(PlatformError) as ig_limit:
                instagram.publish("token", "ig-user", {"variant": {"caption": "Reviewed caption"}}, [
                    {"mime_type": "image/jpeg", "public_url": "https://social.example.test/image.jpg"},
                ])
            self.assertEqual(ig_limit.exception.code, "publishing_limit_reached")
            self.assertEqual(instagram._request_json.call_count, 1)
            self.assertIn("content_publishing_limit", instagram._request_json.call_args.args[1])

            threads = ThreadsProvider()
            threads._request_json = MagicMock(return_value={"data": []})
            with self.assertRaises(PlatformError) as unreadable:
                threads.publish("token", "threads-user", {"variant": {"body": "Reviewed post"}}, [])
            self.assertEqual(unreadable.exception.code, "publishing_limit_unavailable")
            self.assertEqual(threads._request_json.call_count, 1)
            self.assertIn("threads_publishing_limit", threads._request_json.call_args.args[1])

    def test_provider_image_and_video_constraints_fail_before_external_calls(self):
        with runtime_settings():
            instagram = InstagramProvider()
            instagram._request_json = MagicMock()
            with self.assertRaises(PlatformError) as ig_image:
                instagram.publish("token", "ig-user", {"variant": {"caption": "Reviewed caption"}}, [
                    {"mime_type": "image/jpeg", "size_bytes": 8 * 1024 * 1024 + 1,
                     "public_url": "https://social.example.test/image.jpg"},
                ])
            self.assertEqual(ig_image.exception.code, "image_too_large")
            instagram._request_json.assert_not_called()

            threads = ThreadsProvider()
            threads._request_json = MagicMock()
            with self.assertRaises(PlatformError) as thread_video:
                threads.publish("token", "threads-user", {"variant": {"body": "Reviewed post"}}, [{
                    "mime_type": "video/mp4", "size_bytes": 1024, "duration_seconds": 301,
                    "public_url": "https://social.example.test/video.mp4",
                }])
            self.assertEqual(thread_video.exception.code, "video_duration_invalid")
            threads._request_json.assert_not_called()

    def test_tiktok_direct_post_requires_consent_and_private_visibility_for_unaudited_app(self):
        with runtime_settings(audited_tiktok=False):
            provider = TikTokProvider()
            provider.creator_info = MagicMock(return_value={
                "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
                "max_video_post_duration_sec": 60,
            })
            provider._request_json = MagicMock(return_value={"data": {"publish_id": "publish-id-1"}})
            snapshot = {"variant": {"body": "Script", "caption": "Reviewed TikTok caption."}, "tiktok": {
                "privacyLevel": "SELF_ONLY", "consentAt": "2026-09-23T10:00:00+00:00", "allowComment": False,
                "allowDuet": False, "allowStitch": False, "ownBrand": False, "brandedContent": False, "isAigc": True,
            }}
            assets = [{"mime_type": "video/mp4", "duration_seconds": 20, "public_url": "https://social.example.test/video.mp4"}]
            receipt = provider.publish("token", "tiktok-user", snapshot, assets)
            self.assertEqual(receipt.external_id, "publish-id-1")
            sent = provider._request_json.call_args.kwargs["json_body"]
            self.assertEqual(sent["post_info"]["privacy_level"], "SELF_ONLY")
            self.assertTrue(sent["post_info"]["is_aigc"])
            self.assertEqual(sent["source_info"]["source"], "PULL_FROM_URL")

            public = {**snapshot, "tiktok": {**snapshot["tiktok"], "privacyLevel": "PUBLIC_TO_EVERYONE"}}
            with self.assertRaises(PlatformError) as unaudited:
                provider.publish("token", "tiktok-user", public, assets)
            self.assertEqual(unaudited.exception.code, "unaudited_private_only")

            no_consent = {**snapshot, "tiktok": {"privacyLevel": "SELF_ONLY"}}
            with self.assertRaises(PlatformError) as consent:
                provider.publish("token", "tiktok-user", no_consent, assets)
            self.assertEqual(consent.exception.code, "tiktok_consent_required")

    def test_metric_parsing_keeps_provider_measurements_and_empty_is_not_zero(self):
        provider = ThreadsProvider()
        provider._request_json = MagicMock(return_value={"data": [
            {"name": "views", "period": "lifetime", "values": [{"value": 2}, {"value": 19, "end_time": "2026-09-23T00:00:00Z"}]},
            {"name": "likes", "total_value": {"value": 0}},
            {"name": "unavailable", "values": []},
        ]})
        metrics = provider.collect_metrics("token", "owner", "post-id")
        self.assertEqual([(metric.name, metric.value) for metric in metrics], [("views", 19.0), ("likes", 0.0)])
        self.assertEqual(metrics[0].period_end, "2026-09-23T00:00:00Z")
        provider._request_json.return_value = {"data": []}
        self.assertEqual(provider.collect_metrics("token", "owner", "post-id"), [])


if __name__ == "__main__":
    unittest.main()
