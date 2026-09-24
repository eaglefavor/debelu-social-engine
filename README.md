# Debelu Social Engine

Debelu Social Engine is a **single-owner, approval-first** social-content workspace for Debelu Ventures. It supports content planning, review, provider OAuth, uploaded media, a durable publishing outbox, and provider-measured reporting. This is a real server-backed application; it is not a browser-only demo.

**External integrations are not yet live-verified.** The repository contains official-API adapters, but this checkout has no registered production app IDs/secrets, platform app-review approvals, connected accounts, production callback URL, or publicly reachable deployment. Keep the publishing switch off until those prerequisites have been completed and tested against provider test accounts.

## Run locally

Requires Python 3.11+ and `ffprobe` for verified MP4 duration. Docker includes FFmpeg/ffprobe; a local install should provide `ffprobe` on `PATH` before uploading videos intended for Instagram Reels, Threads or TikTok publishing.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` before starting:

- Set a unique `APP_PASSWORD` (at least 12 characters) and a different, random `APP_SECRET` (at least 32 characters).
- Keep `SOCIAL_PUBLISHING_ENABLED=false` until a production HTTPS deployment, provider app review, token encryption key, callback and media delivery have all been configured.
- Leave `AI_API_KEY` blank for manual content entry, or set a server-side key for generation.
- Local SQLite data and media are stored under the ignored `data/` directory.

Then run:

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. Do not expose the local HTTP server to the public internet. The standalone publishing worker can be run separately with `python -m backend.worker`; it will report healthy while idle, but will not send jobs while the social publishing switch is off.

## Docker deployment

Compose runs PostgreSQL, the web/API process and a separate publishing worker. PostgreSQL and uploaded media use persistent named volumes. The web container applies Alembic migrations before starting.

```bash
cp .env.example .env
# Set APP_PASSWORD, APP_SECRET, POSTGRES_PASSWORD and the production settings below.
docker compose up --build -d
docker compose logs -f social-engine publisher-worker
```

Compose binds the web port to `127.0.0.1:8000` by default, so it is not directly internet-facing. Configure a reverse proxy on the host or use an internal container network to reach the service.

For any public deployment:

1. Put the app behind a managed HTTPS reverse proxy and set `COOKIE_SECURE=true`.
2. Set `APP_PUBLIC_URL` to the public HTTPS **origin only** (no path prefix). Provider callback URLs are derived from it as `/api/social/{platform}/callback` and must exactly match each provider console.
3. Set `SOCIAL_TOKEN_ENCRYPTION_KEY` to 64 random hexadecimal characters (32 bytes). Back it up separately: losing it requires every connected account to reconnect.
4. Register and configure each official provider app, allowed redirect, test roles and requested permission reviews. Add the matching server-only client ID/key and secret as an all-or-nothing pair.
5. Make sure the app's public HTTPS origin can serve `/public/media/{asset_id}`. Meta and TikTok fetch media from that URL. Domain/URL-prefix allowlisting may also be required in provider consoles.
6. Configure backups for **both** PostgreSQL and the media volume; restoring one without the other can leave dangling or unavailable assets.
7. After testing with platform test accounts, set `SOCIAL_PUBLISHING_ENABLED=true`. Keep `TIKTOK_DIRECT_POST_AUDITED=false` unless TikTok has actually approved the app for public Direct Post. An unaudited TikTok app is restricted to `SELF_ONLY` by server validation.

Keep `.env` private. AI and social credentials stay on the server; no provider key or social token belongs in frontend code, a public repository, or chat. `AI_BASE_URL` supports OpenAI-compatible Chat Completions services and must use HTTPS except for localhost development.

## Implemented workflow

- **Persistent workspace:** content ideas, platform variants, brand profile, approvals, attached media, schedules, publishing jobs, analytics snapshots and audit history are persisted in the configured database.
- **Authentication:** one workspace password, signed HTTP-only session cookies, configurable expiration, sign-in throttling and same-origin checks on state-changing requests.
- **Human approval:** approval is fingerprinted against the exact copy and attached media. Editing approved content or changing its media invalidates approval and cancels unsubmitted jobs. Published content and unknown provider outcomes are protected against unsafe editing/resubmission.
- **OAuth:** Instagram, Threads and TikTok connect flows use one-time, session-bound OAuth state; TikTok uses PKCE. Account tokens are encrypted at rest using AES-GCM and provider-specific associated data.
- **Media:** bounded multipart uploads, image decode/normalization, MP4 sniffing, ffprobe-verified duration checks for Instagram Reels, Threads videos and TikTok, private authenticated previews and expiring signed public URLs. The server applies request and file-size limits.
- **Durable publishing outbox:** only approved snapshots can be queued. A separate worker atomically claims jobs, keeps leases, checks remote provider status, refreshes expiring credentials, applies bounded retries to known transient failures, and records errors. A timed-out publish with uncertain outcome becomes `UNKNOWN` and is **not** blindly resent. A person must verify it on the platform first. A confirmed, unsubmitted failure can be manually retried.
- **Scheduling:** a selected time with no platform targets is a calendar-only plan. Selecting connected accounts creates one job per platform. “Queue publish” creates a visible, cancellable two-minute outbox window; it is not an immediate synchronous provider call.
- **TikTok controls:** Direct Post requires one duration-verified MP4, fresh creator options, selected privacy and interaction controls, commercial/own-brand disclosures, AIGC disclosure and explicit per-post consent. The API app's audit status is enforced again by the worker.
- **Analytics and reporting:** periodic snapshots use provider-returned measurements only. Missing metrics remain missing, not zero; current reports explicitly state that platform definitions are not directly comparable. Weekly CSV export escapes formula-like values.
- **No unsupervised engagement:** there is no automated reply, comment moderation, client inquiry response, incident response or mass-DM workflow. Sensitive technical/security claims, complaints and incidents still need human review.
- **Health:** `GET /api/health` checks app/database readiness. The Publishing screen separately displays recent publisher-worker heartbeat state.

### Platform scope and prerequisites

Adapters target the official APIs and request only the intended publishing/analytics surface. Availability still depends on app review, access tier, scopes, account type, regional/provider policies and current API behavior.

- **Instagram:** Instagram Login for professional accounts; requests `instagram_business_basic`, `instagram_business_content_publish` and `instagram_business_manage_insights`. This publishing flow supports one JPEG (8 MiB max), one MP4 Reel (verified 3 seconds–15 minutes, 300 MiB max), or a JPEG carousel (2–10). A quota preflight checks Meta's provider-reported rolling publishing limit before making containers; the provider still makes the final decision. [Meta Content Publishing guide](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
- **Threads:** Threads OAuth; requests `threads_basic`, `threads_content_publish` and `threads_manage_insights`. Text is limited to 500 UTF-8 bytes (Threads counts emojis by UTF-8 byte length); JPEG/PNG images are limited to 8 MiB and MP4 video duration is verified at no more than 5 minutes. Before creating containers, the adapter checks the provider's 250 API-published posts per rolling 24-hour limit; it waits 30 seconds, polls container status at most once per minute for up to 5 minutes, and publishes only after `FINISHED`. [Meta Threads API overview](https://developers.facebook.com/documentation/threads/overview)
- **TikTok:** OAuth/PKCE with `user.info.basic`, `video.publish` and `video.list`; Direct Post requires a publicly accessible media URL. The TikTok app audit flag is a server configuration supplied by the operator, **not** an automatic verification of approval. [TikTok Content Posting API](https://developers.tiktok.com/doc/content-posting-api-get-started/)

The implementation handles common provider errors, but real rate limits, app-review decisions, feature access and account-specific cases cannot be asserted without provider credentials and live test accounts. The adapters should be validated against each registered app's current official console/docs before enabling production publishing.

## Scope boundary: single owner, not multi-tenant SaaS

The current deployment is deliberately **one private workspace and one owner password per installation**. Social accounts belong to that installation. It does not provide user registration, role-based access, tenant IDs, per-client isolation, or SaaS account boundaries. Do not expose it as a multi-tenant service or put unrelated clients' accounts in the same instance. Before doing that, implement and test tenant-scoped database access, sessions, OAuth state, media authorization, worker claims, reporting, billing/admin boundaries and tenant-specific encryption/retention. This is a consequential architecture decision, not a UI toggle.

## Test and migration verification

Run the offline/mock-based suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover a fresh migration and adoption of an unversioned Phase 1 database, approval and target gates, OAuth/CSRF/PKCE state handling, encrypted-token lifecycle, declared and chunked upload bounds, signed media URLs, provider request/response mocks, Meta publishing-quota preflights and media constraints, TikTok privacy/consent constraints, idempotent scheduling, worker concurrency/lease recovery, ambiguous outcomes, bounded retries, metrics snapshots and CSV formula escaping. They make **no live provider calls**.

For non-Docker local database upgrades, run `alembic upgrade head` with the intended `.env`/`DATABASE_URL` before starting the app. Production should use PostgreSQL; SQLite is a single-host development option, not a horizontally scaled worker/database configuration.

## API outline

The browser and API are served from the same origin. Main routes include:

- `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
- `GET /api/workspace`, `GET/POST /api/ideas`, `GET/PUT/DELETE /api/ideas/{id}`
- `POST /api/generation/week`; `POST /api/ideas/{id}/submit`, `/approve`, `/schedule`, `/publish`
- `GET /api/social/status`, `POST /api/social/{platform}/connect`, `DELETE /api/social/accounts/{id}`
- `POST /api/assets`, `PUT /api/ideas/{id}/assets/{platform}`
- `GET /api/publishing/jobs`, `POST /api/publishing/jobs/{id}/cancel`, `/retry`
- `GET /api/analytics`, `GET /api/reports/weekly`, `GET /api/reports/weekly.csv`
- `GET /api/ideas/{id}/history`, `GET/PUT /api/brand-profile`

Interactive API documentation is disabled. Private API requests require the workspace session and same-origin checks.
