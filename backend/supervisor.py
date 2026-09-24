"""Process supervisor for single-container deployments.

Why this exists
---------------
``compose.yaml`` runs the web service and the publishing worker as separate
containers sharing a Docker volume. That layout does not map onto managed hosts:
Fly.io volumes attach to exactly one Machine, and a Render disk attaches to one
service. Because the worker reads the media directory that the web service writes
to, splitting them breaks publishing.

``RUN_MODE=all`` runs both processes in one container against one disk, which is
what those hosts can actually provision. ``RUN_MODE=web`` and ``RUN_MODE=worker``
remain available and keep the two-service layout working.

    RUN_MODE=web     migrate, then uvicorn (default)
    RUN_MODE=worker  the publishing worker only (no migrations)
    RUN_MODE=all     migrate, then uvicorn and the worker together

Migrations run only for modes that include the web process, and always before
either process starts, so the worker never sees a stale schema.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("debelu.supervisor")

VALID_MODES = {"web", "worker", "all"}

# How long the web process gets to finish in-flight requests after SIGTERM, and
# how long the worker gets to release its job lease and stop. The worker's own
# graceful stop can legitimately take minutes when a publish call is in flight,
# so it is given the longer budget; compose already grants it 11 minutes.
WEB_STOP_GRACE_SECONDS = 30
WORKER_STOP_GRACE_SECONDS = 600


def run_mode() -> str:
    mode = os.environ.get("RUN_MODE", "web").strip().lower()
    if mode not in VALID_MODES:
        raise SystemExit(f"RUN_MODE must be one of {', '.join(sorted(VALID_MODES))}, not {mode!r}.")
    return mode


def port() -> str:
    # Render, Fly and Heroku-style hosts inject PORT; 8000 is the local default.
    return os.environ.get("PORT", "8000").strip() or "8000"


def web_command() -> list[str]:
    return [
        "uvicorn", "backend.main:app",
        "--host", "0.0.0.0",
        "--port", port(),
        "--proxy-headers",
        # Only trust forwarded headers from the platform's own proxy hop.
        "--forwarded-allow-ips", os.environ.get("FORWARDED_ALLOW_IPS", "*"),
    ]


def worker_command() -> list[str]:
    return [sys.executable, "-m", "backend.worker"]


def migrate() -> None:
    """Apply Alembic migrations, failing the container if they do not succeed."""
    logger.info("Applying database migrations")
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=False)
    if result.returncode != 0:
        raise SystemExit(f"Database migrations failed with exit code {result.returncode}; refusing to start.")


def ensure_writable_directories() -> None:
    """Fail fast, with a fixable message, when the data directories are not writable.

    Docker named volumes inherit ownership from the image, so compose works with
    the non-root image user. Platform volumes (Fly, Render) are mounted
    root-owned and shadow that layout, which leaves uid 10001 unable to write. The
    resulting traceback is otherwise buried under an unrelated upload or backup
    failure much later, so it is checked here instead.
    """
    from backend.config import settings

    problems: list[str] = []
    for label, path in (("MEDIA_DIR", settings.media_dir), ("STAGING_DIR", settings.staging_dir)):
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            probe = path / f".write-probe-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{label} ({path}): {exc.strerror or exc}")
    if problems:
        raise SystemExit(
            "Cannot write to the configured data directories:\n  "
            + "\n  ".join(problems)
            + "\n\nMounted volumes are often root-owned while this image runs as uid 10001."
            "\n  Fly.io:  fly ssh console -C 'chown -R 10001:10001 /app/data'"
            "\n  Render:  mount the disk at /app/data so both MEDIA_DIR and STAGING_DIR live on it"
            "\n  Compose: named volumes inherit ownership from the image and need no change."
        )


def _terminate(process: subprocess.Popen, name: str, grace: int) -> None:
    if process.poll() is not None:
        return
    logger.info("Stopping %s (SIGTERM, %ss grace)", name, grace)
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.warning("%s did not stop within %ss; killing", name, grace)
        process.kill()
        process.wait(timeout=10)


def supervise() -> int:
    """Run the web and worker processes together, stopping both if either exits.

    Returns 0 for a signal-initiated shutdown. Hosts treat a non-zero exit as a
    crash and may restart or alert on it, so a clean SIGTERM must not report
    failure just because the children died with ``-15``.
    """
    web = subprocess.Popen(web_command())
    worker = subprocess.Popen(worker_command())
    stopping = False

    def force_kill() -> None:
        for process in (web, worker):
            if process.poll() is None:
                process.kill()

    def handle_signal(signum, _frame):  # noqa: ANN001 - signal handler signature
        nonlocal stopping
        name = signal.Signals(signum).name
        if stopping:
            # A second signal means the platform is impatient; stop immediately
            # rather than waiting out the remaining grace period.
            logger.warning("Second %s received; forcing shutdown", name)
            force_kill()
            os._exit(1)
        stopping = True
        logger.info("Received %s; shutting down both processes", name)
        # Stop the worker first: it may be mid-publish, and it needs the web
        # process alive to keep serving the media URL the provider is fetching.
        _terminate(worker, "publishing worker", WORKER_STOP_GRACE_SECONDS)
        _terminate(web, "web server", WEB_STOP_GRACE_SECONDS)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # If either process dies on its own, bring the container down rather than
    # limping on with a half-working service.
    exit_code = 0
    try:
        while not stopping:
            web_code, worker_code = web.poll(), worker.poll()
            if web_code is not None:
                exit_code = web_code or 1
                logger.error("Web server exited with code %s", web_code)
                break
            if worker_code is not None:
                exit_code = worker_code or 1
                logger.error("Publishing worker exited with code %s", worker_code)
                break
            time.sleep(0.5)
    finally:
        if not stopping:
            _terminate(worker, "publishing worker", WORKER_STOP_GRACE_SECONDS)
            _terminate(web, "web server", WEB_STOP_GRACE_SECONDS)
    return exit_code


def load_environment() -> None:
    """Load .env before reading RUN_MODE or PORT.

    Functions in backend.config are the only place .env is read, and it happens at
    import time. Without this, a local ``RUN_MODE=all`` in .env would be silently
    ignored and the container would quietly start web-only.
    """
    from backend.config import load_dotenv

    load_dotenv()


def main() -> int:
    load_environment()
    mode = run_mode()
    ensure_writable_directories()
    if mode in {"web", "all"}:
        migrate()
    if mode == "worker":
        logger.info("Starting publishing worker only")
        # exec so the worker is PID-of-record and receives signals directly.
        os.execvp(worker_command()[0], worker_command())
    if mode == "web":
        logger.info("Starting web server on port %s", port())
        os.execvp(web_command()[0], web_command())
    logger.info("Starting web server and publishing worker together (RUN_MODE=all)")
    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())
