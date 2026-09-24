from __future__ import annotations

import logging
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, insert, select, update

from backend.config import settings
from backend.database import engine, metadata, social_accounts, worker_heartbeats
from backend.analytics import collect_due_metrics
from backend.publisher import run_publishing_once
from backend.social_accounts import get_access_token
from backend.social_providers import PlatformError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("debelu.worker")
WORKER_ID = f"worker-{uuid.uuid4().hex[:16]}"
WORKER_COMPONENT = f"publisher:{WORKER_ID}"
_SHUTDOWN = False
_LAST_TOKEN_REFRESH = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _heartbeat(status: str, details: str = "") -> None:
    now = _stamp()
    with engine.begin() as connection:
        exists = connection.execute(select(worker_heartbeats.c.component).where(worker_heartbeats.c.component == WORKER_COMPONENT)).first()
        values = {"worker_id": WORKER_ID, "status": status, "last_seen_at": now, "details": details[:240]}
        if exists:
            connection.execute(update(worker_heartbeats).where(worker_heartbeats.c.component == WORKER_COMPONENT).values(**values))
        else:
            connection.execute(insert(worker_heartbeats).values(component=WORKER_COMPONENT, **values))
        connection.execute(delete(worker_heartbeats).where(
            worker_heartbeats.c.component.like("publisher:%"), worker_heartbeats.c.last_seen_at < _stamp(_now() - timedelta(days=30)),
        ))


def refresh_expiring_tokens() -> dict[str, int]:
    now = _now()
    limit = _stamp(now + timedelta(minutes=35))
    with engine.connect() as connection:
        account_ids = connection.execute(select(social_accounts.c.id).where(
            social_accounts.c.status == "CONNECTED",
            social_accounts.c.access_expires_at.is_not(None),
            social_accounts.c.access_expires_at <= limit,
        ).limit(100)).scalars().all()
    refreshed = failures = 0
    for account_id in account_ids:
        try:
            get_access_token(account_id)
            refreshed += 1
        except PlatformError as exc:
            failures += 1
            logger.warning("Token refresh failed for account %s: %s", account_id, exc.code)
    return {"checked": len(account_ids), "refreshed": refreshed, "failures": failures}


def run_once() -> dict[str, object]:
    if not settings.social_publishing_enabled:
        result: dict[str, object] = {"publishing": {"disabled": 1}, "analytics": {"jobsChecked": 0}, "tokens": {"checked": 0}}
    else:
        result = {
            "publishing": run_publishing_once(WORKER_ID),
            "analytics": collect_due_metrics(),
            "tokens": {"checked": 0},
        }
        global _LAST_TOKEN_REFRESH
        if time.monotonic() - _LAST_TOKEN_REFRESH >= 600:
            result["tokens"] = refresh_expiring_tokens()
            _LAST_TOKEN_REFRESH = time.monotonic()
    _heartbeat("ok", f"jobs={result['publishing'].get('claimed', 0)} analytics={result['analytics'].get('jobsChecked', 0)}")
    return result


def _handle_signal(_signum, _frame):
    global _SHUTDOWN
    _SHUTDOWN = True


def main() -> None:
    metadata.create_all(engine)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    logger.info("Publishing worker started (%s); polling every %.1f seconds", WORKER_ID, settings.worker_poll_seconds)
    while not _SHUTDOWN:
        try:
            result = run_once()
            if result["publishing"].get("claimed") or result["analytics"].get("jobsChecked"):
                logger.info("Worker cycle complete: %s", result)
        except Exception:
            logger.exception("Worker cycle failed")
            try:
                _heartbeat("degraded", "The latest worker cycle failed; check the application logs.")
            except Exception:
                logger.exception("Could not update worker heartbeat")
        time.sleep(settings.worker_poll_seconds)
    _heartbeat("stopped", "Worker shutting down")
    logger.info("Publishing worker stopped")


if __name__ == "__main__":
    main()
