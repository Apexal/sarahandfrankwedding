# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

This is Sarah and Frank's wedding website: a Django 6 / Python 3.14 project managed with `uv`, with two users (us) and a guest list. The Django project package is `sarahandfrankwedding`. It began life as a client-project starter template; the parts of that template that only make sense for a client project (audit log, impersonation, staff roles, public registration, per-user timezones and the profile page, mobile-API tokens, faker seeding, the bootstrap script) were removed on purpose — see PLAN.md — so don't reintroduce them.

Four installed apps hold all custom code: `core` (users, admin, auth, middleware, signals, rate limiter, housekeeping tasks), `messaging` (templated transactional email pipeline), `pages` (the public wedding site: `WeddingSettings` singleton, prose pages, the site-password gate), and `guests` (the guest-list system of record: households, guests, events, RSVPs, save-the-date and RSVP flows, campaigns, dashboard). Each domain app has its own `CLAUDE.md`.

## Common commands

```bash
make setup        # install deps, copy .env, migrate, seed, install Tailwind, install pre-commit
make dev          # runs `make css` (Tailwind one-shot) in background + Django runserver
make css-watch    # Tailwind --watch (use this in a separate terminal during real frontend work; `make dev` does not watch)
make test         # uv run pytest
make lint         # ruff check + ruff format --check
make lint-fix     # ruff check --fix + ruff format
make migrate      # makemigrations + migrate
make seed         # core/management/commands/seed.py — creates the admin/admin superuser and both settings singletons
```

Run a single test: `uv run pytest core/tests.py::TestHealthEndpoint -v` (pytest-django is configured via `pyproject.toml`; settings module is `sarahandfrankwedding.settings`).

## Architecture notes that aren't obvious from one file

- **DEBUG vs ENV are independent switches.** `DEBUG` toggles dev-only behavior (debug toolbar, file-based email backend writing to `sent-emails/`, `ImmediateBackend` for `django.tasks` so jobs run inline). `ENV=production` toggles infra (S3/Tigris storage via `django-storages`, MediaStorage for import/export temp files). Don't conflate them — a staging deploy can have `DEBUG=False` and still need `ENV=production` to get S3.
- **The S3 endpoint setting name is a trap.** django-storages reads `AWS_S3_ENDPOINT_URL`; the env var Fly's Tigris integration injects is `AWS_ENDPOINT_URL_S3`. Transposing them is a silent no-op — `endpoint_url` stays `None`, boto3 falls back to real AWS, and every upload dies at request time on an unresolvable `{bucket}.s3.auto.amazonaws.com`. (`auto` in a hostname is the tell: AWS has no such region.) `core/checks.py` now fails `manage.py check` — and therefore the `entry.sh` deploy — rather than letting it surface on a patient's first upload.
- **Background jobs use `django.tasks` + `steady_queue`.** Define with `@task` (see `messaging/tasks.py`). Recurring jobs use the `recurring_if_supported` wrapper in `core/tasks.py` — it no-ops on `ImmediateBackend` so dev doesn't try to schedule. In production `entry.sh` runs `manage.py steady_queue` as a sidecar process alongside gunicorn; it autodiscovers each app's `tasks.py`. Recurring housekeeping runs nightly: `clearsessions`, `OutboundEmail` pruning (`OUTBOUND_EMAIL_RETENTION_DAYS`, default 90), and the guest-data backup.
- **The database is switched by `DATABASE_URL`.** Unset (local dev) → SQLite at `db.sqlite3` with tuned WAL pragmas; set (staging/production, via `fly postgres attach`) → Postgres parsed by `dj-database-url` with `conn_max_age` + health checks. Tests always force in-memory SQLite regardless of `DATABASE_URL`, so pytest never touches Postgres.
- **The default cache is DatabaseCache** (`django_cache` table, created by `createcachetable` in `make setup`/`make migrate`/`entry.sh` — it is not part of migrations). It must stay a cross-process backend: allauth's rate-limit counters and `WeddingSettings` invalidation depend on the gunicorn workers and the `steady_queue` sidecar seeing the same cache. Tests override it to LocMemCache.
- **`pages.WeddingSettings` is the one cached singleton** (`pages/models.py`). Always read via `WeddingSettings.load()` — it caches and forces `pk=1` on save. `SitePasswordMiddleware` reads it on every non-exempt request, and `core.account_adapter.SiteIdentity` reads `couple_names` from it so allauth's emails are signed the way the site is.
- **Signup is closed, unconditionally.** `CoreAccountAdapter.is_open_for_signup` returns `False`; the only accounts are ours, created by `seed`/`createsuperuser`. allauth exists for login, password reset, and MFA only.
- **Transactional email goes through the `messaging` app** — two-layer templating (code-defined context contracts + DB-stored bodies) with an `OutboundEmail` audit/retry trail. Details in `messaging/CLAUDE.md`.
- **There are no staff groups.** Two superusers; the `Group` admin is unregistered so nothing gets clicked together in the admin. If someone else ever needs read access, give them `is_staff` plus explicit user permissions.
- **Client IPs come from `TrustedClientIPMiddleware`** (first in `MIDDLEWARE`). When `TRUSTED_CLIENT_IP_HEADER` is set (both fly.toml files set `Fly-Client-IP`), it rewrites `REMOTE_ADDR` so allauth rate limits, `core/ratelimit.py`, and structlog `ip` see the real client rather than Fly's proxy. Never set that env var in an environment where clients can reach the app without going through the proxy.
- **Middleware order that matters:** `TrustedClientIPMiddleware` first; `pages.middleware.SitePasswordMiddleware` right after `AuthenticationMiddleware`; `RequireStaffMfaMiddleware` after `AuthenticationMiddleware` and `MessageMiddleware`; `django_structlog.middlewares.RequestMiddleware` last.
- **Custom user lives at `core.User`** (`AUTH_USER_MODEL = "core.User"`). It extends `AbstractUser` + `TimestampedModel`, normalizes phone numbers in `save()`, and `email` is required & unique. `REQUIRED_FIELDS = ["email"]` but username is still the `USERNAME_FIELD`.
- **Login is email-based with mandatory verification** (`ACCOUNT_LOGIN_METHODS = {"email"}`, `ACCOUNT_EMAIL_VERIFICATION = "mandatory"`). Usernames only matter internally/in the admin. Every user is created outside allauth (seed, `createsuperuser`, tests) and needs a verified `EmailAddress` row to log in — `seed.py` and the `conftest.py` fixtures create one; a production `createsuperuser` will be emailed a verification link on first login.
- **Staff MFA is enforced.** `allauth.mfa` provides TOTP, passkeys (WebAuthn), and recovery codes; `MFA_PASSKEY_LOGIN_ENABLED` also allows passwordless passkey login. `RequireStaffMfaMiddleware` redirects staff/superusers with no TOTP or WebAuthn authenticator to `/accounts/2fa/` to enroll (recovery codes alone don't count). Controlled by `MFA_REQUIRED_FOR_STAFF` — on by default, off in `.env.example` and under pytest (`TestRequireStaffMfa` opts back in per-test). `DEBUG=True` sets `MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN` so passkeys work on plain-http localhost.
- **Type hint for views: `core.types.AppRequest`** — extends `HttpRequest` with `htmx: HtmxDetails` and a typed `user: User`. Use it as the request annotation in views to get correct typing for `request.user` and `request.htmx`.
- **Admin login is hardened** by `allauth.account.decorators.secure_admin_login` (applied in `core/admin.py`). The default `Group` admin is unregistered. In the user admin, non-superuser staff get `is_staff`/`is_superuser`/`groups`/`user_permissions` as read-only and cannot change or delete superuser accounts — keep those guards when customizing `UserAdmin`.
- **Logging is structlog + django-structlog.** `LOGGING` and `structlog.configure(...)` live at the top of `sarahandfrankwedding/settings.py`; the renderer is chosen by `ENV` — key/value for `ENV=development`, JSON everywhere else. `django_structlog.middlewares.RequestMiddleware` (last in `MIDDLEWARE`) binds `request_id`/`user_id`/`ip` and emits `request_started`/`request_finished`. `sarahandfrankwedding/logging.py` adds a `bind_extra_request_metadata` receiver that also binds `username`, `email`, `is_staff`; it's imported from `sarahandfrankwedding/__init__.py` so the receiver registers before Django loads settings. Get loggers with `structlog.get_logger(__name__)` and pass structured kwargs (`logger.info("event_name", order_id=42)`) rather than f-strings. Stdlib `logging.getLogger()` calls still work — they route through the same `ProcessorFormatter`. Levels: `LOG_LEVEL` (root, default INFO) and `DJANGO_LOG_LEVEL` (the noisy `django` logger, default WARNING).

## Wedding-site notes

- **One singleton.** `pages.WeddingSettings` holds the wedding's content and switches (couple names, date, city, site password, `codes_frozen`, `save_the_date_open`, RSVP open/deadline/copy, album URL, registry links, contact email). Read it via `.load()`; `pages.context_processors.wedding` exposes it as `wedding` in every template.
- **The site password gate** is `pages.middleware.SitePasswordMiddleware` (right after `AuthenticationMiddleware`). Blank `site_password` = gate off, and it stays blank on production until the invitations mail. The session stores `WeddingSettings.site_password_fingerprint` under `SESSION_UNLOCK_KEY`, so rotating the password re-locks everyone. Save-the-date pages call `pages.middleware.unlock_site(request)`: holding that link is the credential. Compare passwords with `WeddingSettings.check_site_password()` (stripped, casefolded), never by hand.
- **Guest session keys** live in `guests/session.py` (`rsvp_household_id`, `rsvp_candidates`). Nothing else identifies a guest.
- **`core/ratelimit.py`** is the per-IP limiter for every public POST (`@ratelimit("10/15m")`): fixed windows in the shared `DatabaseCache`, a 429 page over the limit, no-op under pytest like the old API throttles. Test `is_rate_limited()` directly or `override_settings(TESTING=False)`.
- **Staging email safety.** `EMAIL_RECIPIENT_ALLOWLIST` (env, comma-separated) makes `messaging/tasks.py` deliver any off-list recipient to the first allowed address with the original recipient in the subject; the `OutboundEmail` row keeps the real address. `core/checks.py` fails `manage.py check` (and so the deploy) when a Fly app named `*staging*` leaves it empty. Never set it on production.
- **`CSRF_TRUSTED_ORIGINS` is derived from `APP_BASE_URL`** plus the `.fly.dev` host, so the custom domain must be in `APP_BASE_URL` or every guest form post fails.
- **Email templates for the wedding are registered from `guests/email_contexts.py`** via `messaging.template_definitions.register_template_definition` in `GuestsConfig.ready()`, and their default bodies are seeded by a data migration in `guests/migrations/`, so a fresh clone sends mail before anyone opens the admin. `messaging` never imports `guests`.
- **Bounces** arrive through anymail's Resend tracking webhook at `/anymail/resend/tracking/` (`ANYMAIL_WEBHOOK_SECRET` as basic auth in the URL, `RESEND_SIGNING_SECRET` from Resend); `guests/signals.py` sets `Household.email_bounced_at`, which every campaign audience excludes.
- **Backups.** `core.tasks.backup_guest_data` writes `dumpdata guests pages` to `backups/guests-<date>.json` in the default storage nightly (03:40) and keeps 30. Restore with `loaddata`.
- **The guest list rules** (household is the unit of invitation, pending is the absence of an `Rsvp` row, `address_confirmed_at` is the address state, invite codes freeze once links are sent) are in `guests/CLAUDE.md`.

## Frontend

Tailwind + DaisyUI + HTMX. Build setup, template layout, and CSS-watch details live in `.claude/rules/frontend.md` (loaded automatically when working under `templates/` or `static/`).

**Rich text is CKEditor 5** (`django-ckeditor-5`). Declare fields as `CKEditor5Field`; toolbars are defined by `CKEDITOR_5_CONFIGS` in `sarahandfrankwedding/settings.py` — `default` for short fields, `extends` for long-form prose. Nothing in the starter uses it yet; it's wired so a domain app can just add the field. Two rules:

- **Render saved HTML inside a `.ck-content` container with `|safe`.** `static/css/ckeditor-content.css` re-establishes the heading/list/blockquote styling that Tailwind preflight strips, and is loaded both in the editor (`CKEDITOR_5_CUSTOM_CSS`) and on the page, so authors see what readers see.
- **In your own templates use `{% ckeditor_media %}` (from `core_filters`), never `{{ form.media }}`.** Executing `bundle.js` twice resets `window.editors`/`ckeditorRegisterCallback` and orphans JS hooks attached to the first bundle. The tag renders once per request and returns nothing on later calls. The Django admin renders its own media and needs nothing.

The `extends` toolbar includes `sourceEditing` (raw HTML), which is safe only while the field is staff-only. Before exposing an `extends` editor to non-staff authors, drop `sourceEditing` and sanitize on output — bodies are rendered with `|safe`.

## Deployment

Fly.io single-machine deploy (production + staging apps) backed by Postgres; `entry.sh` supervises migrations, the `steady_queue` worker, and gunicorn. Details in `.claude/rules/deploy.md` (loaded automatically when touching `fly*.toml`, `entry.sh`, `Dockerfile`, or `.github/`).

## Conventions

- **Conventional Commits are enforced** via `conventional-pre-commit` (commit-msg stage) and feed `release-please` for versioning. `feat:` → minor, `fix:` → patch, `feat!:`/`BREAKING CHANGE:` → major. See `RELEASING.md`.
- **Pre-commit hooks** run ruff (lint + format), djlint (Django templates), and on `post-merge` automatically run `migrate` and rebuild Tailwind. Don't bypass with `--no-verify`.
- **Ruff config** is in `pyproject.toml`: line length 120, target py314, isort knows `core`, `messaging`, `pages`, and `guests` as first-party.
- **Tests** live as `tests.py` or `test_*.py`; classes start with `Test`, functions with `test_`. Shared fixtures live in the root `conftest.py`: `user`, `staff_user`, `admin_user` (verified users; plaintext password on `.raw_password`), and `user_client` (logged-in regular user). Use pytest-django's `client`/`admin_client` for HTTP tests. The cache is cleared automatically between tests (LocMemCache would otherwise leak the `WeddingSettings` singleton across tests), and warnings are errors (`filterwarnings = ["error"]`).

## Error Tracking

- There is no error-tracking service: the structlog output in the Fly logs is the record. Every module gets its logger with `structlog.get_logger(__name__)`.
- Any exception that is caught and handled (for example: converted to an HTTP response, recorded on a row, or skipped with `continue`) must be logged from inside the `except` block with `logger.exception("<event_name>", ...)`, which attaches the traceback. Use `logger.warning` for expected-but-notable paths (a CSRF failure, a rejected RSVP lookup, an email the backend refused).
- Pass low-cardinality context as kwargs (`error_type=type(exc).__name__`, the handler or task name, relevant ids) rather than formatting it into the message.
- Significant domain events (`site_unlocked`, `address_submitted`, `rsvp_submitted`, `campaign_sent`, `email_sent`, `email_bounced`) are `logger.info` calls with the same kind of kwargs, so the behaviour stays visible in production.
- Do not add noisy logging for expected control-flow exceptions (for example normal validation failures or expected `DoesNotExist` branches) unless explicitly required.

## Git

- Only commit when explicitly told to.
- Use conventional commit format for all commit messages (e.g., `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`)
- If a pre-commit hook rejects a commit, fix the issues and retry the commit
