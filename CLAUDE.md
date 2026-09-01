# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

This is a Django 6 / Python 3.14 starter template managed with `uv`. The Django project package is `starter_app` — that name is a placeholder rewritten by `setup_project.sh` (along with `<client>`, `<client legal name>`, `<description>`) on first use. If you see those placeholders in real client work, the bootstrap step was skipped.

There are three installed apps that hold all custom code: `core` (users, admin, auth, middleware, signals, home/profile views), `messaging` (templated transactional email pipeline), and `agreements` (versioned legal agreements + consent trail). Add new domain apps as siblings.

## Common commands

```bash
make setup        # install deps, copy .env, migrate, seed, install Tailwind, install pre-commit
make dev          # runs `make css` (Tailwind one-shot) in background + Django runserver
make css-watch    # Tailwind --watch (use this in a separate terminal during real frontend work; `make dev` does not watch)
make test         # uv run pytest
make lint         # ruff check + ruff format --check
make lint-fix     # ruff check --fix + ruff format
make migrate      # makemigrations + migrate
make seed         # core/management/commands/seed.py — creates admin/admin superuser + faker users
```

Run a single test: `uv run pytest core/tests.py::TestHealthEndpoint -v` (pytest-django is configured via `pyproject.toml`; settings module is `starter_app.settings`).

## Architecture notes that aren't obvious from one file

- **DEBUG vs ENV are independent switches.** `DEBUG` toggles dev-only behavior (debug toolbar, file-based email backend writing to `sent-emails/`, `ImmediateBackend` for `django.tasks` so jobs run inline). `ENV=production` toggles infra (S3/Tigris storage via `django-storages`, MediaStorage for import/export temp files). Don't conflate them — a staging deploy can have `DEBUG=False` and still need `ENV=production` to get S3.
- **The S3 endpoint setting name is a trap.** django-storages reads `AWS_S3_ENDPOINT_URL`; the env var Fly's Tigris integration injects is `AWS_ENDPOINT_URL_S3`. Transposing them is a silent no-op — `endpoint_url` stays `None`, boto3 falls back to real AWS, and every upload dies at request time on an unresolvable `{bucket}.s3.auto.amazonaws.com`. (`auto` in a hostname is the tell: AWS has no such region.) `core/checks.py` now fails `manage.py check` — and therefore the `entry.sh` deploy — rather than letting it surface on a patient's first upload.
- **Background jobs use `django.tasks` + `steady_queue`.** Define with `@task` (see `messaging/tasks.py`). Recurring jobs use the `recurring_if_supported` wrapper in `core/tasks.py` — it no-ops on `ImmediateBackend` so dev doesn't try to schedule. In production `entry.sh` runs `manage.py steady_queue` as a sidecar process alongside gunicorn; it autodiscovers each app's `tasks.py`. Recurring housekeeping runs nightly: `clearsessions`, auditlog pruning (`AUDITLOG_RETENTION_DAYS`, default 365), and `OutboundEmail` pruning (`OUTBOUND_EMAIL_RETENTION_DAYS`, default 90).
- **The database is switched by `DATABASE_URL`.** Unset (local dev) → SQLite at `db.sqlite3` with tuned WAL pragmas; set (staging/production, via `fly postgres attach`) → Postgres parsed by `dj-database-url` with `conn_max_age` + health checks. Tests always force in-memory SQLite regardless of `DATABASE_URL`, so pytest never touches Postgres.
- **The default cache is DatabaseCache** (`django_cache` table, created by `createcachetable` in `make setup`/`make migrate`/`entry.sh` — it is not part of migrations). It must stay a cross-process backend: allauth's rate-limit counters and `AppSettings` invalidation depend on the gunicorn workers and the `steady_queue` sidecar seeing the same cache. Tests override it to LocMemCache.
- **`AppSettings` is a cached singleton** (`core/models.py`). Always read via `AppSettings.load()` — it caches and forces `pk=1` on save. It's exposed on every request through `core.context_processors.global_context` and is read by `MaintenanceModeMiddleware` on every request.
- **Whether signups are open is `AppSettings.allow_registration`, not an env var**, and it **defaults to off** — closing an open deployment is a click rather than a deploy. One switch for both halves: `CoreAccountAdapter.is_open_for_signup` closes the allauth signup page (and the navbar/home links, which read `app_settings.allow_registration`), and `/auth/register` answers 403. Tests are closed by default too, so anything walking a signup path opens it with `conftest.set_registration` / the `open_registration` fixture.
- **Maintenance mode** short-circuits all non-staff requests with a 503 (only `/health/` is whitelisted). Toggle via the `AppSettings` admin.
- **Impersonation flow.** Superusers POST to `core_user_impersonate` (admin user change page). The original user id is stashed in `request.session[SESSION_IMPERSONATOR_USER_ID_KEY]`. `ImpersonationAuditMiddleware` then rewrites the auditlog actor back to the impersonator so audit trails attribute changes correctly — it must stay ordered *after* `auditlog.middleware.AuditlogMiddleware` in `MIDDLEWARE`. Stop via `core:stop_impersonation`.
- **Transactional email goes through the `messaging` app** — two-layer templating (code-defined context contracts + DB-stored bodies) with an `OutboundEmail` audit/retry trail. Details in `messaging/CLAUDE.md`.
- **Legal agreements live in the `agreements` app** — immutable versioned text plus a per-user `UserAgreement` consent trail. `AgreementGateMiddleware` redirects any signed-in non-staff user who owes a required agreement to `/agreements/pending/`; it's a no-op until an `Agreement` is published, so a fresh project is unaffected. Public text is served at `/agreements/<key>/` for footer links. Details in `agreements/CLAUDE.md`.
- **Staff groups are code-defined, not admin-edited.** `core/roles.py` declares `GROUP_PERMISSIONS` (group name → `"app_label.codename"` permissions); `manage.py sync_groups` syncs the DB to match it exactly (run by `make setup`/`make migrate`/`entry.sh`; `--prune` deletes undeclared groups). The `Group` admin stays unregistered — change groups in code so environments can't drift. Division of labor: a role/type field on `User` decides what the app shows a kind of user; groups/permissions decide what staff can do (chiefly in the admin); queryset filtering decides which rows anyone can touch.
- **Auditlog is wired into the User and AppSettings models** via `auditlog.register(...)` at the bottom of `core/models.py`. New auditable models follow the same pattern (add `AuditlogHistoryField` + register) and the admin uses `AuditlogHistoryAdminMixin` to expose a history view.
- **PostHog is everywhere.** Auth events fire from `core/signals.py`; impersonation, profile edit, and email send/failure events fire from their respective views/tasks. When adding new significant user actions, follow the same `posthog.capture(...)` pattern (and use `posthog.new_context()` + `identify_context("system")` from background tasks where there's no request user).
- **Client IPs come from `TrustedClientIPMiddleware`** (first in `MIDDLEWARE`). When `TRUSTED_CLIENT_IP_HEADER` is set (both fly.toml files set `Fly-Client-IP`), it rewrites `REMOTE_ADDR` so allauth rate limits, auditlog `remote_addr`, and structlog `ip` see the real client rather than Fly's proxy. Never set that env var in an environment where clients can reach the app without going through the proxy.
- **Custom user lives at `core.User`** (`AUTH_USER_MODEL = "core.User"`). It extends `AbstractUser` + `TimestampedModel`, normalizes phone numbers in `save()`, and `email` is required & unique. `REQUIRED_FIELDS = ["email"]` but username is still the `USERNAME_FIELD`.
- **Login is email-based with mandatory verification** (`ACCOUNT_LOGIN_METHODS = {"email"}`, `ACCOUNT_EMAIL_VERIFICATION = "mandatory"`). Usernames are generated by the allauth adapter at signup and only matter internally/in the admin. Users created outside allauth (seed, `createsuperuser`, tests) need a verified `EmailAddress` row to log in — `seed.py` and the `conftest.py` fixtures create one; a production `createsuperuser` will be emailed a verification link on first login.
- **Staff MFA is enforced.** `allauth.mfa` provides TOTP, passkeys (WebAuthn), and recovery codes; `MFA_PASSKEY_LOGIN_ENABLED` also allows passwordless passkey login. `RequireStaffMfaMiddleware` redirects staff/superusers with no TOTP or WebAuthn authenticator to `/accounts/2fa/` to enroll (recovery codes alone don't count). Controlled by `MFA_REQUIRED_FOR_STAFF` — on by default, off in `.env.example` and under pytest (`TestRequireStaffMfa` opts back in per-test). Skipped during impersonation. `DEBUG=True` sets `MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN` so passkeys work on plain-http localhost.
- **Type hint for views: `core.types.AppRequest`** — extends `HttpRequest` with `htmx: HtmxDetails` and a typed `user: User`. Use it as the request annotation in views to get correct typing for `request.user` and `request.htmx`.
- **Admin login is hardened** by `allauth.account.decorators.secure_admin_login` (applied in `core/admin.py`). The default `Group` admin is unregistered. In the user admin, non-superuser staff get `is_staff`/`is_superuser`/`groups`/`user_permissions` as read-only and cannot change or delete superuser accounts — keep those guards when customizing `UserAdmin`.
- **Logging is structlog + django-structlog.** `LOGGING` and `structlog.configure(...)` live at the top of `starter_app/settings.py`; the renderer is chosen by `ENV` — key/value for `ENV=development`, JSON everywhere else. `django_structlog.middlewares.RequestMiddleware` (last in `MIDDLEWARE`) binds `request_id`/`user_id`/`ip` and emits `request_started`/`request_finished`. `starter_app/logging.py` adds a `bind_extra_request_metadata` receiver that also binds `username`, `email`, `is_staff`; it's imported from `starter_app/__init__.py` so the receiver registers before Django loads settings. Get loggers with `structlog.get_logger(__name__)` and pass structured kwargs (`logger.info("event_name", order_id=42)`) rather than f-strings. Stdlib `logging.getLogger()` calls still work — they route through the same `ProcessorFormatter`. Levels: `LOG_LEVEL` (root, default INFO) and `DJANGO_LOG_LEVEL` (the noisy `django` logger, default WARNING).

## Frontend

Tailwind + DaisyUI + HTMX. Build setup, template layout, and CSS-watch details live in `.claude/rules/frontend.md` (loaded automatically when working under `templates/` or `static/`).

**Rich text is CKEditor 5** (`django-ckeditor-5`). Declare fields as `CKEditor5Field`; toolbars are defined by `CKEDITOR_5_CONFIGS` in `starter_app/settings.py` — `default` for short fields, `extends` for long-form prose. Nothing in the starter uses it yet; it's wired so a domain app can just add the field. Two rules:

- **Render saved HTML inside a `.ck-content` container with `|safe`.** `static/css/ckeditor-content.css` re-establishes the heading/list/blockquote styling that Tailwind preflight strips, and is loaded both in the editor (`CKEDITOR_5_CUSTOM_CSS`) and on the page, so authors see what readers see.
- **In your own templates use `{% ckeditor_media %}` (from `core_filters`), never `{{ form.media }}`.** Executing `bundle.js` twice resets `window.editors`/`ckeditorRegisterCallback` and orphans JS hooks attached to the first bundle. The tag renders once per request and returns nothing on later calls. The Django admin renders its own media and needs nothing.

The `extends` toolbar includes `sourceEditing` (raw HTML), which is safe only while the field is staff-only. Before exposing an `extends` editor to non-staff authors, drop `sourceEditing` and sanitize on output — bodies are rendered with `|safe`.

## Mobile API

`core/api/` is a django-ninja JSON API mounted at `/api/`, serving the paired Expo app (`rn-expo-starter`). It is deliberately separate from the HTMX views: those authenticate with allauth sessions, this authenticates with opaque bearer tokens, because a mobile client has no cookie jar. Both halves share one user table, so the same credentials work in both places.

- **Tokens are hashed, never stored raw.** `core.models.HashedToken` (abstract) → `AccessToken` (1 hour) and `RefreshToken` (90 days). `AccessToken.resolve(raw)` is an indexed lookup on the SHA-256 hash; an unsalted hash is fine here because the raw value is 256 bits of `secrets.token_urlsafe`.
- **Refresh is single-use with family-wide breach response.** The two tokens minted by one login share a `family` UUID. `/auth/refresh` rotates (revokes the presented token, issues a new pair in the same family). Replaying an already-rotated token means someone has a copy, so it revokes the whole family — attacker *and* legitimate holder — rather than merely refusing. Tested in `core/api/tests.py::TestRefresh`.
- **Registration verifies with a code, not a link.** allauth emails a confirmation URL, which an app can't complete, so `/auth/register` creates an inactive user and emails a 6-digit `EmailVerificationCode` (hashed with `make_password`, 10-minute TTL, 5-attempt cap, prior codes invalidated on re-issue). `/auth/verify-email` activates the user *and* marks the allauth `EmailAddress` verified, so the same person can then sign in on the web. Gated by `AppSettings.allow_registration`, same as web signup.
- **Auth responses are deliberately uninformative.** Register always returns the same body whether the address is new, unverified, or taken; verify returns one neutral 422 for every failure mode. The real reason goes to the audit log via `core.audit.record_auth_event`, not to the client. Don't "improve" these error messages — the enumeration resistance is the feature.
- **Per-IP throttles** on every unauthenticated auth endpoint (`core/api/throttling.py`). They no-op under pytest, so tests assert on behavior rather than on request count.
- **`core/api/auth.py::TokenAuth` is the single choke point** every authenticated endpoint passes through. If your app should only serve a subset of users, put that check there rather than in each handler.
- **Email goes through file-based templates**, not `messaging`'s DB-stored ones (`core/api/emails.py`) — a fresh clone has no `EmailTemplateVersion` rows, and registration has to work before anyone opens the admin. Delivery still lands in the `OutboundEmail` audit trail and the rate-limited `email` queue.
- **Account deletion** (`core/api/account.py`) is a scheduled request with a `core.tasks.GRACE_PERIOD` window, swept nightly by `sweep_due_account_deletions`. Requesting revokes all tokens, so canceling requires a fresh login — that's the proof of control. The default execution is a hard delete; `core/deletion.py` exposes `@register_deletion_handler` for apps that need to scrub or reassign their own rows first, and `AccountDeletionRequest.user` is `SET_NULL` so the record that a deletion happened outlives the account.
- **Regenerating the client:** the Expo app runs `npm run gen:api` against `/api/openapi.json`, so a change to a schema class or route is a breaking change to the app. The schema is open when `ENV=development` and behind an admin login otherwise.

## Deployment

Fly.io single-machine deploy (production + staging apps) backed by Postgres; `entry.sh` supervises migrations, the `steady_queue` worker, and gunicorn. Details in `.claude/rules/deploy.md` (loaded automatically when touching `fly*.toml`, `entry.sh`, `Dockerfile`, or `.github/`).

## Conventions

- **Conventional Commits are enforced** via `conventional-pre-commit` (commit-msg stage) and feed `release-please` for versioning. `feat:` → minor, `fix:` → patch, `feat!:`/`BREAKING CHANGE:` → major. See `RELEASING.md`.
- **Pre-commit hooks** run ruff (lint + format), djlint (Django templates), and on `post-merge` automatically run `migrate` and rebuild Tailwind. Don't bypass with `--no-verify`.
- **Ruff config** is in `pyproject.toml`: line length 120, target py314, isort knows `core` and `messaging` as first-party.
- **Tests** live as `tests.py` or `test_*.py`; classes start with `Test`, functions with `test_`. Shared fixtures live in the root `conftest.py`: `user`, `staff_user`, `admin_user` (verified users; plaintext password on `.raw_password`), `user_client` (logged-in regular user), and `posthog_exceptions` (captures `posthog.capture_exception` calls). Use pytest-django's `client`/`admin_client` for HTTP tests. The cache is cleared automatically between tests (LocMemCache would otherwise leak the `AppSettings` singleton across tests), and warnings are errors (`filterwarnings = ["error"]`).

## Error Tracking

- PostHog exception autocapture is enabled, but it does not cover every handled error path.
- Any exception that is caught and handled (for example: logged, converted to an HTTP response, or skipped with `continue`) must also be sent to PostHog via `posthog.capture_exception(...)`.
- Include a stable `distinct_id` (user id when available, otherwise a service identifier) and low-cardinality context properties (for example `error_type`, endpoint/handler/task name, relevant ids).
- Do not add noisy capture for expected control-flow exceptions (for example normal validation failures or expected `DoesNotExist` branches) unless explicitly required.

## Git

- Only commit when explicitly told to.
- Use conventional commit format for all commit messages (e.g., `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`)
- If a pre-commit hook rejects a commit, fix the issues and retry the commit
