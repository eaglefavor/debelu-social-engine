from __future__ import annotations

import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select, update

from test_support import TEST_DATA  # noqa: F401
from backend.database import analytics_snapshots, content_ideas, publish_jobs, engine
from backend.publisher import claim_due_jobs, run_publishing_once
from backend.social_providers import Metric, PlatformError, PublishReceipt
from backend.analytics import collect_job_metrics, query_analytics, weekly_report
from pipeline_support import ApiCase, future_iso, register_account, runtime_settings


class FakeProvider:
    def __init__(self, *, receipt: PublishReceipt | None = None, publish_error: PlatformError | None = None,
                 poll_error: PlatformError | None = None, metrics: list[Metric] | None = None):
        self.receipt = receipt or PublishReceipt("provider-post-1", "https://www.threads.net/@tester/post/example")
        self.publish_error = publish_error
        self.poll_error = poll_error
        self.metrics = metrics or []
        self.publish_calls = 0
        self.poll_calls = 0
        self.metric_calls = 0

    def publish(self, access_token, external_account_id, snapshot, assets):
        self.publish_calls += 1
        if self.publish_error:
            raise self.publish_error
        return self.receipt

    def poll_publish(self, access_token, external_account_id, provider_post_id):
        self.poll_calls += 1
        if self.poll_error:
            raise self.poll_error
        return self.receipt

    def collect_metrics(self, access_token, external_account_id, provider_post_id):
        self.metric_calls += 1
        return self.metrics


class PublisherWorkerTests(ApiCase):
    def create_due_job(self):
        account = register_account("threads")
        idea = self.create_approved("threads")
        scheduled = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
            "scheduledFor": future_iso(180),
            "targets": [{"platform": "threads", "accountId": account["id"]}],
        })
        self.assertEqual(scheduled.status_code, 200, scheduled.text)
        job_id = scheduled.json()["scheduledJobs"][0]["id"]
        due = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
        with engine.begin() as connection:
            connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
                scheduled_for=due, next_attempt_at=due,
            ))
        return idea, account, job_id

    def run_worker(self, provider):
        with patch("backend.publisher.get_access_token", return_value="mock-provider-token"), \
             patch("backend.publisher.get_provider", return_value=provider):
            return run_publishing_once("test-worker", limit=10)

    def test_publish_success_rolls_up_and_next_metrics_are_scheduled(self):
        with runtime_settings():
            idea, _account, job_id = self.create_due_job()
            provider = FakeProvider(receipt=PublishReceipt("threads-post-123", "https://www.threads.net/@tester/post/123"))
            result = self.run_worker(provider)
            self.assertEqual(result["claimed"], 1)
            self.assertEqual(result["completed"], 1)
            self.assertEqual(provider.publish_calls, 1)
            with engine.connect() as connection:
                job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
                updated_idea = connection.execute(select(content_ideas).where(content_ideas.c.id == idea["id"])).mappings().one()
            self.assertEqual(job["status"], "PUBLISHED")
            self.assertEqual(job["attempt_count"], 1)
            self.assertEqual(job["provider_post_id"], "threads-post-123")
            self.assertIsNotNone(job["next_metrics_at"])
            self.assertEqual(updated_idea["status"], "PUBLISHED")
            self.assertEqual(updated_idea["published_url"], "https://www.threads.net/@tester/post/123")
            edited = self.client.put(f"/api/ideas/{idea['id']}", json={"topic": "Change after publication is not allowed"})
            self.assertEqual(edited.status_code, 409)

    def test_ambiguous_publish_is_never_automatically_retried_or_disconnected(self):
        with runtime_settings():
            idea, account, job_id = self.create_due_job()
            provider = FakeProvider(publish_error=PlatformError(
                "threads", "read_timeout", "The provider request timed out.", ambiguous=True,
            ))
            result = self.run_worker(provider)
            self.assertEqual(result["claimed"], 1)
            self.assertEqual(provider.publish_calls, 1)
            with engine.connect() as connection:
                job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(job["status"], "UNKNOWN")
            self.assertIsNone(job["next_attempt_at"])
            self.assertEqual(self.run_worker(provider)["claimed"], 0)
            self.assertEqual(provider.publish_calls, 1)
            edited = self.client.put(f"/api/ideas/{idea['id']}", json={"topic": "Unsafe edit after unknown outcome"})
            self.assertEqual(edited.status_code, 409)
            disconnected = self.client.delete(f"/api/social/accounts/{account['id']}")
            self.assertEqual(disconnected.status_code, 409, disconnected.text)
            retried = self.client.post(f"/api/publishing/jobs/{job_id}/retry")
            self.assertEqual(retried.status_code, 409, retried.text)

    def test_retry_after_rate_limit_is_bounded_then_succeeds_once(self):
        with runtime_settings(worker_max_attempts=3):
            _idea, _account, job_id = self.create_due_job()
            limited = FakeProvider(publish_error=PlatformError(
                "threads", "rate_limited", "The platform rate limited this request.", retryable=True, retry_after=60,
            ))
            self.run_worker(limited)
            with engine.connect() as connection:
                retry = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(retry["status"], "RETRY")
            self.assertEqual(retry["attempt_count"], 1)
            self.assertGreater(datetime.fromisoformat(retry["next_attempt_at"]), datetime.now(timezone.utc))
            self.assertEqual(self.run_worker(limited)["claimed"], 0)
            due = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec="seconds")
            with engine.begin() as connection:
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(next_attempt_at=due))
            success = FakeProvider(receipt=PublishReceipt("post-after-retry", "https://www.threads.net/@tester/post/retry"))
            self.run_worker(success)
            self.assertEqual(success.publish_calls, 1)
            with engine.connect() as connection:
                published = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(published["status"], "PUBLISHED")
            self.assertEqual(published["attempt_count"], 2)

    def test_remote_status_poll_timeout_stops_retries(self):
        with runtime_settings(worker_max_attempts=3):
            _idea, _account, job_id = self.create_due_job()
            provider = FakeProvider(receipt=PublishReceipt("remote-threads-id", complete=False, status_detail="Processing"))
            self.run_worker(provider)
            with engine.connect() as connection:
                remote = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(remote["status"], "REMOTE_PROCESSING")
            self.assertEqual(remote["provider_post_id"], "remote-threads-id")
            past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
            with engine.begin() as connection:
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
                    status="REMOTE_PROCESSING", remote_deadline_at=past, next_attempt_at=past,
                ))
            polling_error = FakeProvider(poll_error=PlatformError(
                "threads", "http_429", "Status endpoint rate limited.", retryable=True, ambiguous=True, retry_after=30,
            ))
            self.run_worker(polling_error)
            self.assertEqual(polling_error.publish_calls, 0)
            self.assertEqual(polling_error.poll_calls, 1)
            with engine.connect() as connection:
                terminal = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(terminal["status"], "NEEDS_ATTENTION")
            self.assertEqual(terminal["last_error_code"], "provider_poll_timeout")
            self.assertIsNone(terminal["next_attempt_at"])

    def test_expired_lease_recovery_marks_unknown_without_resubmitting(self):
        with runtime_settings():
            _idea, _account, job_id = self.create_due_job()
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
            with engine.begin() as connection:
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
                    status="PROCESSING", lease_owner="dead-worker", lease_until=past,
                ))
            provider = FakeProvider()
            result = self.run_worker(provider)
            self.assertEqual(result["recovered"], 1)
            self.assertEqual(result["claimed"], 0)
            self.assertEqual(provider.publish_calls, 0)
            with engine.connect() as connection:
                recovered = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(recovered["status"], "UNKNOWN")

    def test_claim_is_compare_and_set_and_two_workers_do_not_claim_the_same_job(self):
        with runtime_settings():
            _idea, _account, job_id = self.create_due_job()
            first = claim_due_jobs("worker-a", limit=10)
            second = claim_due_jobs("worker-b", limit=10)
            self.assertEqual(first, [job_id])
            self.assertEqual(second, [])
            with engine.connect() as connection:
                row = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(row["lease_owner"], "worker-a")

    def test_manual_retry_only_requeues_known_failures_with_matching_approval(self):
        with runtime_settings():
            idea, _account, job_id = self.create_due_job()
            with engine.begin() as connection:
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(
                    status="FAILED", last_error_code="provider_rejected", last_error="Definitive provider rejection.",
                    scheduled_for=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"),
                ))
                connection.execute(update(content_ideas).where(content_ideas.c.id == idea["id"]).values(status="FAILED"))
            retried = self.client.post(f"/api/publishing/jobs/{job_id}/retry")
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(retried.json()["status"], "RETRY")
            with engine.connect() as connection:
                row = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(row["attempt_count"], 0)
            self.assertIsNotNone(row["next_attempt_at"])

    def test_provider_metrics_are_idempotently_refreshed_and_weekly_reported(self):
        with runtime_settings():
            idea, account, job_id = self.create_due_job()
            provider = FakeProvider(receipt=PublishReceipt("threads-measured-id", "https://www.threads.net/@tester/post/measured"))
            self.run_worker(provider)
            due_metrics = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec="seconds")
            with engine.begin() as connection:
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(next_metrics_at=due_metrics))
            first = FakeProvider(metrics=[Metric("views", 20), Metric("likes", 3)])
            with patch("backend.analytics.get_access_token", return_value="mock-token"), \
                 patch("backend.analytics.get_provider", return_value=first):
                count = collect_job_metrics(job_id)
            self.assertEqual(count, 2)
            self.assertEqual(first.metric_calls, 1)
            with engine.connect() as connection:
                saved_job = connection.execute(select(publish_jobs).where(publish_jobs.c.id == job_id)).mappings().one()
            self.assertEqual(collect_job_metrics(job_id), 0)  # next observation is not due yet
            due = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec="seconds")
            with engine.begin() as connection:
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(next_metrics_at=due))
            second = FakeProvider(metrics=[Metric("views", 28), Metric("likes", 4), Metric("invalid", float("nan"))])
            with patch("backend.analytics.get_access_token", return_value="mock-token"), \
                 patch("backend.analytics.get_provider", return_value=second):
                updated_count = collect_job_metrics(job_id)
            self.assertEqual(updated_count, 2)
            with engine.connect() as connection:
                rows = connection.execute(select(analytics_snapshots).where(analytics_snapshots.c.publish_job_id == job_id)).mappings().all()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["metric"]: row["value"] for row in rows}, {"views": 28.0, "likes": 4.0})
            analytics = query_analytics(days=30, platform="threads")
            self.assertTrue(any(row["ideaId"] == idea["id"] and row["value"] == 28.0 for row in analytics["metrics"]))
            today = datetime.now(timezone.utc).date()
            week_start = (today - timedelta(days=today.weekday())).isoformat()
            report = weekly_report(week_start)
            self.assertGreaterEqual(report["publishedCount"], 1)
            self.assertEqual(report["metricsByPlatform"]["threads"]["views"], 28.0)
            self.assertEqual(account["platform"], "threads")
            with engine.connect() as connection:
                self.assertEqual(connection.execute(select(publish_jobs.c.status).where(publish_jobs.c.id == job_id)).scalar_one(), "PUBLISHED")
            invalid_week = self.client.get("/api/reports/weekly?week=not-a-date")
            self.assertEqual(invalid_week.status_code, 422)

    def test_weekly_csv_escapes_formula_like_topic_text(self):
        with runtime_settings():
            account = register_account("threads")
            topic = f"=SUM(1,1)-{uuid.uuid4().hex[:5]}"
            idea = self.create_approved("threads", topic=topic)
            scheduled = self.client.post(f"/api/ideas/{idea['id']}/schedule", json={
                "scheduledFor": future_iso(180),
                "targets": [{"platform": "threads", "accountId": account["id"]}],
            }).json()
            job_id = scheduled["scheduledJobs"][0]["id"]
            due = (datetime.now(timezone.utc) - timedelta(seconds=4)).isoformat(timespec="seconds")
            with engine.begin() as connection:
                connection.execute(update(publish_jobs).where(publish_jobs.c.id == job_id).values(scheduled_for=due))
            self.run_worker(FakeProvider(receipt=PublishReceipt("csv-post", "https://www.threads.net/@tester/post/csv")))
            today = datetime.now(timezone.utc).date()
            start = (today - timedelta(days=today.weekday())).isoformat()
            csv_response = self.client.get(f"/api/reports/weekly.csv?week={start}")
            self.assertEqual(csv_response.status_code, 200, csv_response.text)
            self.assertIn("'" + topic, csv_response.text)
            self.assertIn("text/csv", csv_response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
